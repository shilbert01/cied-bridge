using System;
using System.IO;
using System.IO.Ports;
using System.Threading;

// KardiSynch Serial Bridge — Windows companion for the Pi Zero CDC-ACM presenter.
//
// Receives framed file data over a COM port (USB CDC-ACM gadget) and writes
// complete files into an output folder that KardiSynch watches. Sends an ACK
// frame back so the Pi only marks a file delivered once it landed here CRC-OK.
//
// Wire format — length-prefixed frames:
//   [SYNC "KS" 2B][TYPE 1B][LENGTH 4B LE][PAYLOAD ...][CRC32 4B LE]

namespace KardiSynchSerialBridge
{
    static class Program
    {
        static readonly byte[] SYNC = { 0x4B, 0x53 }; // "KS"
        const byte PKT_FILE_START = 0x01;
        const byte PKT_FILE_DATA = 0x02;
        const byte PKT_FILE_END = 0x03;
        const byte PKT_ACK = 0x05;

        static string OutputDir = @"C:\KardiSynch\incoming";
        static string? PortName = null;

        static void Main(string[] args)
        {
            for (int i = 0; i < args.Length; i++)
            {
                if ((args[i] == "--output" || args[i] == "-o") && i + 1 < args.Length)
                    OutputDir = args[++i];
                else if ((args[i] == "--port" || args[i] == "-p") && i + 1 < args.Length)
                    PortName = args[++i];
                else if (!args[i].StartsWith("-"))
                    PortName ??= args[i];
            }
            Directory.CreateDirectory(OutputDir);

            Console.WriteLine("KardiSynch Serial Bridge");
            Console.WriteLine($"Output: {OutputDir}");

            while (true)
            {
                try
                {
                    string port = PortName ?? FindPort();
                    Console.WriteLine($"Opening {port} ...");
                    using var sp = new SerialPort(port, 115200)
                    {
                        ReadTimeout = Timeout.Infinite,
                        WriteTimeout = 5000,
                        DtrEnable = true,
                        RtsEnable = true
                    };
                    sp.Open();
                    Console.WriteLine($"Connected: {port}");
                    RunReceiveLoop(sp);
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"Error: {ex.Message} — retrying in 2s");
                    Thread.Sleep(2000);
                }
            }
        }

        static string FindPort()
        {
            // Auto-detect: look for any COM port; prefer higher-numbered ones
            // (COM3+ are typically USB-added).
            while (true)
            {
                var ports = SerialPort.GetPortNames();
                if (ports.Length > 0)
                {
                    Array.Sort(ports);
                    var pick = ports[ports.Length - 1];
                    Console.WriteLine($"Auto-detected: {pick} (from {string.Join(", ", ports)})");
                    return pick;
                }
                Console.Write(".");
                Thread.Sleep(1000);
            }
        }

        static void RunReceiveLoop(SerialPort sp)
        {
            string? name = null;
            uint expectedSize = 0, expectedCrc = 0;
            MemoryStream? buffer = null;

            while (sp.IsOpen)
            {
                // Read until we see SYNC bytes
                int b0 = sp.ReadByte();
                if (b0 != SYNC[0]) continue;
                int b1 = sp.ReadByte();
                if (b1 != SYNC[1]) continue;

                // Read header: type(1) + length(4) = 5 bytes
                var hdr = ReadExact(sp, 5);
                byte type = hdr[0];
                uint length = BitConverter.ToUInt32(hdr, 1);
                if (length > 16 * 1024 * 1024)
                {
                    // Sanity check — skip garbage
                    continue;
                }

                var payload = ReadExact(sp, (int)length);
                var crcBytes = ReadExact(sp, 4);
                uint crcRecv = BitConverter.ToUInt32(crcBytes, 0);
                uint crcCalc = Crc32(payload);
                if (crcRecv != crcCalc)
                {
                    Console.WriteLine($"Frame CRC mismatch (type={type}), skipping");
                    continue;
                }

                switch (type)
                {
                    case PKT_FILE_START:
                        expectedSize = BitConverter.ToUInt32(payload, 0);
                        expectedCrc = BitConverter.ToUInt32(payload, 4);
                        name = System.Text.Encoding.UTF8.GetString(payload, 8, payload.Length - 8);
                        buffer = new MemoryStream();
                        Console.WriteLine($"Receiving: {name} ({expectedSize} bytes)");
                        break;

                    case PKT_FILE_DATA:
                        if (buffer == null) break;
                        int remaining = (int)(expectedSize - buffer.Length);
                        if (remaining > 0)
                            buffer.Write(payload, 0, Math.Min(remaining, payload.Length));
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

                        SendAck(sp, ok, actualCrc);
                        buffer.Dispose();
                        buffer = null;
                        name = null;
                        break;
                }
            }
        }

        static byte[] ReadExact(SerialPort sp, int n)
        {
            var buf = new byte[n];
            int off = 0;
            while (off < n)
            {
                int got = sp.Read(buf, off, n - off);
                off += got;
            }
            return buf;
        }

        static void WriteAtomic(string name, byte[] data)
        {
            string safe = Path.GetFileName(name);
            string finalPath = Path.Combine(OutputDir, safe);
            string tmp = finalPath + ".part";
            File.WriteAllBytes(tmp, data);
            if (File.Exists(finalPath)) File.Delete(finalPath);
            File.Move(tmp, finalPath);
        }

        static void SendAck(SerialPort sp, bool ok, uint crc)
        {
            var payload = new byte[5];
            payload[0] = (byte)(ok ? 1 : 0);
            BitConverter.GetBytes(crc).CopyTo(payload, 1);

            uint payloadCrc = Crc32(payload);
            using var ms = new MemoryStream();
            ms.Write(SYNC, 0, 2);
            ms.WriteByte(PKT_ACK);
            ms.Write(BitConverter.GetBytes((uint)payload.Length), 0, 4);
            ms.Write(payload, 0, payload.Length);
            ms.Write(BitConverter.GetBytes(payloadCrc), 0, 4);
            var frame = ms.ToArray();
            try { sp.Write(frame, 0, frame.Length); }
            catch (Exception ex) { Console.WriteLine($"ACK write failed: {ex.Message}"); }
        }

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
