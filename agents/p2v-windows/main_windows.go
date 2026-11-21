//go:build windows

package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"strings"
	"syscall"
	"unsafe"
)

var kernel = syscall.NewLazyDLL("kernel32.dll")

func winCall(name string, args ...uintptr) error {
	r, _, err := kernel.NewProc(name).Call(args...)
	if r == 0 { return fmt.Errorf("%s: %w", name, err) }
	return nil
}
func ownConsole() bool {
	var ids [2]uint32
	n, _, _ := kernel.NewProc("GetConsoleProcessList").Call(uintptr(unsafe.Pointer(&ids[0])), 2)
	return n == 1
}
func elevated() bool {
	r, _, _ := syscall.NewLazyDLL("shell32.dll").NewProc("IsUserAnAdmin").Call()
	return r != 0
}
func elevate() error {
	exe, err := os.Executable(); if err != nil { return err }
	args := make([]string, 0, len(os.Args)-1)
	for _, arg := range os.Args[1:] { args = append(args, syscall.EscapeArg(arg)) }
	verb, _ := syscall.UTF16PtrFromString("runas")
	file, err := syscall.UTF16PtrFromString(exe); if err != nil { return err }
	params, err := syscall.UTF16PtrFromString(strings.Join(args, " ")); if err != nil { return err }
	r, _, _ := syscall.NewLazyDLL("shell32.dll").NewProc("ShellExecuteW").Call(0, uintptr(unsafe.Pointer(verb)), uintptr(unsafe.Pointer(file)), uintptr(unsafe.Pointer(params)), 0, 1)
	if r <= 32 { return errors.New("Administrator permission is required; UAC elevation was cancelled or failed") }
	return nil
}
func registryString(name string) (string, error) {
	path, _ := syscall.UTF16PtrFromString(`SOFTWARE\Microsoft\Windows NT\CurrentVersion`)
	var key syscall.Handle
	if err := syscall.RegOpenKeyEx(syscall.HKEY_LOCAL_MACHINE, path, 0, syscall.KEY_READ|syscall.KEY_WOW64_64KEY, &key); err != nil { return "", err }
	defer syscall.RegCloseKey(key)
	field, _ := syscall.UTF16PtrFromString(name)
	var typ uint32
	var data [512]uint16
	size := uint32(unsafe.Sizeof(data))
	err := syscall.RegQueryValueEx(key, field, nil, &typ, (*byte)(unsafe.Pointer(&data[0])), &size)
	if err != nil { return "", err }
	if typ != syscall.REG_SZ { return "", errors.New("unexpected registry value type") }
	return syscall.UTF16ToString(data[:]), nil
}

// MIB_IF_ROW2's fixed prefix, followed by room for the remaining SDK fields.
// HardwareInterface is bit 0 of InterfaceAndOperStatusFlags. This excludes
// software adapters (VPNs, bridges, loopbacks) even when they have Ethernet MACs.
type ifRow struct {
	Luid uint64
	Index uint32
	Guid [16]byte
	Alias [257]uint16
	Description [257]uint16
	PhysicalAddressLength uint32
	PhysicalAddress [32]byte
	PermanentPhysicalAddress [32]byte
	Mtu uint32
	Type uint32
	TunnelType uint32
	MediaType uint32
	PhysicalMediumType uint32
	AccessType uint32
	DirectionType uint32
	Flags byte
	Rest [1024]byte
}
func collectInventory() (inventory, error) {
	var m inventory
	var err error
	m.Hostname, err = os.Hostname(); if err != nil { return m, err }
	count, _, _ := kernel.NewProc("GetActiveProcessorCount").Call(0xffff) // ALL_PROCESSOR_GROUPS
	if count == 0 { return m, errors.New("cannot determine logical CPU count") }
	m.CPUs = int(count)
	var memory struct {
		Length, Load uint32
		TotalPhys, AvailPhys, TotalPageFile, AvailPageFile, TotalVirtual, AvailVirtual, AvailExtendedVirtual uint64
	}
	memory.Length = uint32(unsafe.Sizeof(memory))
	if err = winCall("GlobalMemoryStatusEx", uintptr(unsafe.Pointer(&memory))); err != nil { return m, err }
	m.MemoryMB = memory.TotalPhys / (1<<20)
	var firmware uint32
	if err = winCall("GetFirmwareType", uintptr(unsafe.Pointer(&firmware))); err != nil { return m, err }
	switch firmware { case 1: m.Firmware = "bios"; case 2: m.Firmware = "uefi"; default: return m, errors.New("unknown firmware type") }
	m.OS, err = registryString("ProductName"); if err != nil { return m, fmt.Errorf("read Windows ProductName: %w", err) }
	version, _ := registryString("DisplayVersion")
	if version == "" { version, _ = registryString("ReleaseId") }
	build, _ := registryString("CurrentBuildNumber")
	m.OS = strings.TrimSpace(m.OS+" "+version+" (build "+build+")")
	m.MACs = []string{}
	nics, err := net.Interfaces(); if err != nil { return m, err }
	getIf := syscall.NewLazyDLL("iphlpapi.dll").NewProc("GetIfEntry2")
	for _, nic := range nics {
		if nic.Flags&net.FlagUp == 0 || len(nic.HardwareAddr) != 6 { continue }
		row := ifRow{Index: uint32(nic.Index)}
		status, _, _ := getIf.Call(uintptr(unsafe.Pointer(&row)))
		// If Windows cannot describe the port, keep its MAC rather than fail the whole copy.
		if status != 0 || row.Flags&1 != 0 { m.MACs = append(m.MACs, nic.HardwareAddr.String()) }
	}
	for i := 0; i < 256; i++ {
		name := fmt.Sprintf("PhysicalDrive%d", i)
		f, openErr := os.Open(`\\.\`+name)
		if openErr != nil {
			if errors.Is(openErr, syscall.ERROR_FILE_NOT_FOUND) || errors.Is(openErr, syscall.ERROR_PATH_NOT_FOUND) || errors.Is(openErr, syscall.Errno(21)) { continue }
			return m, fmt.Errorf("open %s: %w", name, openErr)
		}
		var size int64
		var returned uint32
		err = syscall.DeviceIoControl(syscall.Handle(f.Fd()), 0x7405c, nil, 0, (*byte)(unsafe.Pointer(&size)), 8, &returned, nil) // IOCTL_DISK_GET_LENGTH_INFO
		if err != nil { f.Close(); return m, fmt.Errorf("get size of %s: %w", name, err) }
		var geometry struct { Cylinders int64; MediaType, TracksPerCylinder, SectorsPerTrack, BytesPerSector uint32 }
		err = syscall.DeviceIoControl(syscall.Handle(f.Fd()), 0x70000, nil, 0, (*byte)(unsafe.Pointer(&geometry)), uint32(unsafe.Sizeof(geometry)), &returned, nil)
		f.Close()
		if err != nil { return m, fmt.Errorf("get sector size of %s: %w", name, err) }
		if size <= 0 { continue }
		if geometry.BytesPerSector == 0 || size%int64(geometry.BytesPerSector) != 0 { return m, fmt.Errorf("invalid sector geometry for %s", name) }
		m.Disks = append(m.Disks, disk{Name: name, Size: size, sector: geometry.BytesPerSector})
	}
	if len(m.Disks) == 0 || len(m.Disks) > 16 { return m, errors.New("manager requires between 1 and 16 disks") }
	return m, nil
}

func run(input *bufio.Reader) error {
	c, err := executableConfig()
	if err != nil && !errors.Is(err, errNoTrailer) { return err }
	// Only use flags as credentials when there is no embedded configuration.
	var flags config
	options := flag.NewFlagSet("ibhics-p2v", flag.ContinueOnError)
	options.StringVar(&flags.Server, "server", "", "HTTPS manager origin")
	options.StringVar(&flags.Session, "session", "", "P2V session ID")
	options.StringVar(&flags.Token, "token", "", "P2V session token (keep private)")
	options.StringVar(&flags.Pin, "pin", "", "base64 SHA256 of manager SPKI")
	yes := options.Bool("yes", false, "skip copy confirmation")
	replicate := options.Bool("replicate", false, "send a 4 MiB hash delta round")
	if err := options.Parse(os.Args[1:]); err != nil { return err }
	if len(options.Args()) != 0 { return errors.New("unexpected positional arguments") }
	if errors.Is(err, errNoTrailer) { c = flags }
	client, err := c.client(); if err != nil { return err }
	m, err := collectInventory(); if err != nil { return err }
	fmt.Printf("ibHiCS P2V: %s | %d CPUs | %d MB RAM | %s | %s\n", m.Hostname, m.CPUs, m.MemoryMB, m.Firmware, m.OS)
	for _, d := range m.Disks { fmt.Printf("  %s: %.2f GiB\n", d.Name, float64(d.Size)/(1<<30)) }
	fmt.Println("LIVE COPY WITHOUT VSS: stop databases and services that write heavily before continuing. Data can change during copying.")
	if !*yes {
		fmt.Printf("Copy these disks to %s? [y/N] ", c.Server)
		answer, _ := input.ReadString('\n')
		if !strings.EqualFold(strings.TrimSpace(answer), "y") { return errors.New("copy cancelled") }
	}
	if *replicate {
        var sources []io.ReaderAt
        for _, d := range m.Disks {
            f, err := os.Open(`\\.\`+d.Name)
            if err != nil { return errors.New("cannot open replication disk") }
            defer f.Close()
            sources = append(sources, f)
        }
        if err := replicateDisks(client, c, m, sources); err != nil { return err }
        fmt.Println("Replication round committed. Run --replicate again for the next delta.")
        return nil
    }
	body, err := json.Marshal(m); if err != nil { return err }
	if err = send(client, c, "POST", "/inventory", bytes.NewReader(body), false); err != nil { return err }
	for i, d := range m.Disks {
		f, err := os.Open(`\\.\`+d.Name); if err != nil { return fmt.Errorf("open %s: %w", d.Name, err) }
		err = uploadDisk(client, c, d, i, f)
		f.Close()
		if err != nil { return err }
	}
	if err = send(client, c, "POST", "/finish", nil, false); err != nil { return err }
	fmt.Println("Done. Open Migrate > Physical machines in ibHiCS to import this machine.")
	return nil
}
func main() {
	pause := ownConsole()
	input := bufio.NewReader(os.Stdin)
	code := 0
	if !elevated() {
		if err := elevate(); err != nil { fmt.Fprintln(os.Stderr, err); code = 1 } else { return }
	} else if err := run(input); err != nil { fmt.Fprintln(os.Stderr, "P2V failed:", err); code = 1 }
	if pause { fmt.Print("Press Enter to close..."); input.ReadString('\n') }
	os.Exit(code)
}
