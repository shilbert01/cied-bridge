// test_send — simulate a CIED programmer writing files to the sender dongle.
//
// Auto-detects the "CIED Sender" USB drive, creates two dummy PDF files,
// then polls the drive every second to verify continuous access through
// a transfer cycle.
//
// Build:  GOOS=windows GOARCH=amd64 go build -o test_send.exe test_send.go
// Usage:  test_send.exe [DRIVE_LETTER]

package main

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

var dummyPDF = []byte(
	"%PDF-1.4\n" +
		"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n" +
		"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n" +
		"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n" +
		"xref\n0 4\n" +
		"0000000000 65535 f \n" +
		"0000000009 00000 n \n" +
		"0000000058 00000 n \n" +
		"0000000115 00000 n \n" +
		"trailer<</Size 4/Root 1 0 R>>\n" +
		"startxref\n183\n%%EOF\n")

var files = []string{"test_report_A.pdf", "test_report_B.pdf"}

func findDrive(label string) string {
	kernel32 := syscall.NewLazyDLL("kernel32.dll")
	getDriveType := kernel32.NewProc("GetDriveTypeW")
	getVolumeInfo := kernel32.NewProc("GetVolumeInformationW")

	volName := make([]uint16, 261)

	for c := 'D'; c <= 'Z'; c++ {
		root, _ := syscall.UTF16PtrFromString(fmt.Sprintf("%c:\\", c))
		dt, _, _ := getDriveType.Call(uintptr(unsafe.Pointer(root)))
		if dt != 2 { // DRIVE_REMOVABLE
			continue
		}
		ok, _, _ := getVolumeInfo.Call(
			uintptr(unsafe.Pointer(root)),
			uintptr(unsafe.Pointer(&volName[0])), 261,
			0, 0, 0, 0, 0,
		)
		if ok != 0 {
			name := syscall.UTF16ToString(volName)
			if strings.EqualFold(name, label) {
				return fmt.Sprintf("%c:", c)
			}
		}
	}
	return ""
}

func listDir(dir string) map[string]bool {
	entries := make(map[string]bool)
	d, err := os.Open(dir)
	if err != nil {
		return entries
	}
	defer d.Close()
	names, _ := d.Readdirnames(-1)
	for _, n := range names {
		entries[n] = true
	}
	return entries
}

func sortedKeys(m map[string]bool) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func main() {
	var drive string
	if len(os.Args) > 1 {
		drive = strings.TrimRight(os.Args[1], ":\\") + ":"
	} else {
		fmt.Println("Searching for volume 'CIED Sender' ...")
		drive = findDrive("CIED Sender")
		if drive == "" {
			fmt.Println("ERROR: sender dongle not found. Plug it in or pass the drive letter.")
			os.Exit(1)
		}
	}

	root := drive + "\\"
	fmt.Printf("Using drive %s\n", root)

	for _, name := range files {
		path := filepath.Join(root, name)
		fmt.Printf("  Writing %s (%d bytes) ...\n", path, len(dummyPDF))
		if err := os.WriteFile(path, dummyPDF, 0644); err != nil {
			fmt.Printf("  ERROR: %v\n", err)
			os.Exit(1)
		}
	}
	fmt.Printf("Done — %d files written.\n\n", len(files))

	fmt.Println("Monitoring drive (Ctrl+C to stop):")
	fmt.Println("  The sender dongle should remain accessible throughout a transfer.")
	fmt.Println("  After pressing BOOT on the sender, files should move to done/.")
	fmt.Println()

	prev := listDir(root)
	fmt.Printf("  Current files: %v\n", sortedKeys(prev))

	for {
		time.Sleep(1 * time.Second)
		cur := listDir(root)

		for _, name := range sortedKeys(cur) {
			if !prev[name] {
				fmt.Printf("  [%s] + %s\n", time.Now().Format("15:04:05"), name)
			}
		}
		for _, name := range sortedKeys(prev) {
			if !cur[name] {
				fmt.Printf("  [%s] - %s\n", time.Now().Format("15:04:05"), name)
			}
		}
		prev = cur
	}
}
