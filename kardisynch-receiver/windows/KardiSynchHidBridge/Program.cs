using System;
using System.IO;
using System.Linq;
using System.Threading;
using HidSharp;

// KardiSynch HID Bridge — Windows companion for the Pi Zero HID presenter.
//
// Reassembles the 64-byte HID report stream sent by kardisynch-receiver's
// hid_sender.py into whole files under an output folder that KardiSynch watches,
// and sends an ACK output report back so the Pi only marks a file delivered once
// it has landed here CRC-OK. If we never ACK (app closed, PC asleep), the Pi's
// hub redelivers the file — nothing is silently lost.
//
// No driver install: Windows has a built-in HID class driver; HidSharp talks to
// it directly.

namespace KardiSynchHidBridge
{
    static class Program
    {
        // Must match gadget-hid.sh (idVendor/idProduct).
        const int VendorId = 0x1d6b;
        const int ProductId = 0x0104;

        // Wire protocol (see hid_sender.py). Byte 0 of each 64-byte report is a
        // constant marker; the real HID Report ID (buffer[0] from HidSharp) is 0.
        const byte MARKER = 0x01;
        const byte PKT_FILE_START = 0x01;
        const byte PKT_FILE_DATA = 0x02;
        const byte PKT_FILE_END = 0x03;
        const byte PKT_ACK = 0x05;

        const int ReportSize = 64;
        const int PayloadSize = 60;

        static string OutputDir = @"C:\KardiSynch\incoming";

        static void Main(string[] args)
        {
            if (args.Length > 0)
                OutputDir = args[0];
            Directory.CreateDirectory(OutputDir);

            Console.WriteLine("KardiSynch HID Bridge");
            Console.WriteLine($"Output: {OutputDir}");
            Console.WriteLine($"Waiting for HID device {VendorId:X4}:{ProductId:X4} ...");

            while (true)
            {
                try
                {
                    var device = DeviceList.Local
                        .GetHidDevices(VendorId, ProductId)
                        .FirstOrDefault();
                    if (device == null)
                    {
                        Thread.Sleep(1000);
                        continue;
                    }

                    Console.WriteLine($"Connected: {device.GetFriendlyName()}");
                    using var stream = device.Open();
                    RunReceiveLoop(stream);
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"Error: {ex.Message} — retrying");
                    Thread.Sleep(2000);
                }
            }
        }

        static void RunReceiveLoop(HidStream stream)
        {
            stream.ReadTimeout = Timeout.Infinite;

            string name = null;
            uint expectedSize = 0, expectedCrc = 0;
            MemoryStream buffer = null;

            // HidSharp read buffers include a leading Report ID byte (report[0]).
            var report = new byte[ReportSize + 1];

            while (true)
            {
                int n = stream.Read(report, 0, report.Length);
                if (n < 5) continue;

                // Skip the Report ID byte when present (n == 65). Our 64 data
                // bytes then start at 'off'.
                int off = (n >= ReportSize + 1) ? 1 : 0;
                if (report[off] != MARKER) continue; // not one of our reports

                byte type = report[off + 1];
                var payload = new byte[PayloadSize];
                Array.Copy(report, off + 4, payload, 0, PayloadSize);

                switch (type)
                {
                    case PKT_FILE_START:
                        expectedSize = BitConverter.ToUInt32(payload, 0);
                        expectedCrc = BitConverter.ToUInt32(payload, 4);
                        int z = Array.IndexOf<byte>(payload, 0, 8);
                        if (z < 0) z = PayloadSize;
                        name = System.Text.Encoding.UTF8.GetString(payload, 8, z - 8);
                        buffer = new MemoryStream();
                        Console.WriteLine($"Receiving: {name} ({expectedSize} bytes)");
                        break;

                    case PKT_FILE_DATA:
                        if (buffer == null) break;
                        int remaining = (int)(expectedSize - buffer.Length);
                        if (remaining > 0)
                            buffer.Write(payload, 0, Math.Min(remaining, PayloadSize));
                        break;

                    case PKT_FILE_END:
                        if (buffer == null || name == null) break;
                        var data = buffer.ToArray();
                        uint actualCrc = Crc32(data);
                        uint endCrc = BitConverter.ToUInt32(payload, 0);
                        bool ok = actualCrc == expectedCrc && actualCrc == endCrc;

                        if (ok)
                        {
                            WriteAtomic(name, data);
                            Console.WriteLine($"Saved: {name} ({data.Length} bytes, CRC OK)");
                        }
                        else
                        {
                            Console.WriteLine($"CRC MISMATCH {name}: " +
                                $"expected={expectedCrc:X8} end={endCrc:X8} actual={actualCrc:X8}");
                        }

                        SendAck(stream, ok, actualCrc);
                        buffer.Dispose();
                        buffer = null;
                        name = null;
                        break;
                }
            }
        }

        // Write to a temp name then move into place, so KardiSynch's watcher never
        // sees a half-written file.
        static void WriteAtomic(string name, byte[] data)
        {
            string safe = Path.GetFileName(name);
            string finalPath = Path.Combine(OutputDir, safe);
            string tmp = finalPath + ".part";
            File.WriteAllBytes(tmp, data);
            if (File.Exists(finalPath)) File.Delete(finalPath);
            File.Move(tmp, finalPath);
        }

        static void SendAck(HidStream stream, bool ok, uint crc)
        {
            // Output report: [reportId=0][marker][PKT_ACK][seq=0,0][status][crc32]
            var outLen = stream.Device.GetMaxOutputReportLength();
            if (outLen <= 0) outLen = ReportSize + 1;
            var rep = new byte[outLen];
            rep[0] = 0x00;            // Report ID
            rep[1] = MARKER;
            rep[2] = PKT_ACK;
            rep[3] = 0; rep[4] = 0;   // seq (unused)
            rep[5] = (byte)(ok ? 1 : 0);
            BitConverter.GetBytes(crc).CopyTo(rep, 6);
            try { stream.Write(rep, 0, rep.Length); }
            catch (Exception ex) { Console.WriteLine($"ACK write failed: {ex.Message}"); }
        }

        // CRC32 (IEEE, reflected) — matches Python zlib.crc32.
        static readonly uint[] Table = BuildTable();
        static uint[] BuildTable()
        {
            var t = new uint[256];
            for (uint i = 0; i < 256; i++)
            {
                uint c = i;
                for (int k = 0; k < 8; k++)
                    c = (c & 1) != 0 ? 0xEDB88320 ^ (c >> 1) : c >> 1;
                t[i] = c;
            }
            return t;
        }
        static uint Crc32(byte[] data)
        {
            uint crc = 0xFFFFFFFF;
            foreach (byte b in data)
                crc = Table[(crc ^ b) & 0xFF] ^ (crc >> 8);
            return crc ^ 0xFFFFFFFF;
        }
    }
}
