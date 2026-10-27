// test_receive — monitor the receiver dongle for incoming files.
//
// Auto-detects the "CIED Receiver" USB drive, watches the inbox/ folder,
// and reports whenever a new file appears. Handles the brief USB reconnect
// that occurs after each transfer.
//
// Build:  GOOS=windows GOARCH=amd64 go build -o test_receive.exe test_receive.go
// Usage:  test_receive.exe [DRIVE_LETTER]

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

type fileEntry struct {
	name string
	size int64
}

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

func listFiles(dir string) map[string]int64 {
	entries := make(map[string]int64)
	d, err := os.Open(dir)
	if err != nil {
		return entries
	}
	defer d.Close()
	infos, _ := d.Readdir(-1)
	for _, info := range infos {
		if !info.IsDir() {
			entries[info.Name()] = info.Size()
		}
	}
	return entries
}

func sortedKeys(m map[string]int64) []string {
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
		fmt.Println("Searching for volume 'CIED Receiver' ...")
		drive = findDrive("CIED Receiver")
		if drive == "" {
			fmt.Println("ERROR: receiver dongle not found. Plug it in or pass the drive letter.")
			os.Exit(1)
		}
	}

	root := drive + "\\"
	inbox := filepath.Join(root, "inbox")
	fmt.Printf("Using drive %s\n", root)

	if info, err := os.Stat(inbox); err != nil || !info.IsDir() {
		fmt.Printf("WARNING: %s not found — watching root instead\n", inbox)
		inbox = root
	}

	fmt.Printf("Watching %s for new files (Ctrl+C to stop):\n\n", inbox)

	known := listFiles(inbox)
	if len(known) > 0 {
		fmt.Println("  Existing files in inbox:")
		for _, name := range sortedKeys(known) {
			fmt.Printf("    %s (%d bytes)\n", name, known[name])
		}
	} else {
		fmt.Println("  inbox is empty — waiting for transfers ...")
	}

	for {
		time.Sleep(1 * time.Second)
		cur := listFiles(inbox)

		for _, name := range sortedKeys(cur) {
			if _, exists := known[name]; !exists {
				ts := time.Now().Format("15:04:05")
				fmt.Printf("  [%s] RECEIVED: %s (%d bytes)\n", ts, name, cur[name])
			}
		}
		for _, name := range sortedKeys(known) {
			if _, exists := cur[name]; !exists {
				ts := time.Now().Format("15:04:05")
				fmt.Printf("  [%s] REMOVED:  %s\n", ts, name)
			}
		}
		known = cur
	}
}
