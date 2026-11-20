package main

import (
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

const trailerMarker = "\n#IBHICS-P2V#"
const trailerLimit = 64 * 1024
var errNoTrailer = errors.New("no embedded configuration")

type config struct {
	Server string `json:"server"`
	Session string `json:"session"`
	Token string `json:"token"`
	Pin string `json:"pin"`
}
type disk struct {
	Name string `json:"name"`
	Size int64 `json:"size_bytes"`
	sector uint32
}
type inventory struct {
	Hostname string `json:"hostname"`
	CPUs int `json:"cpus"`
	MemoryMB uint64 `json:"memory_mb"`
	Firmware string `json:"firmware"`
	OS string `json:"os"`
	MACs []string `json:"macs"`
	Disks []disk `json:"disks"`
}

// Only inspect a bounded tail; never load the executable into memory.
func readTrailer(r io.ReaderAt, size int64) (config, error) {
	n := int64(trailerLimit)
	if size < n { n = size }
	if n < 0 { return config{}, errors.New("invalid executable size") }
	b := make([]byte, n)
	if _, err := r.ReadAt(b, size-n); err != nil && err != io.EOF { return config{}, err }
	i := bytes.LastIndex(b, []byte(trailerMarker))
	if i < 0 { return config{}, errNoTrailer }
	b = b[i+len(trailerMarker):]
	if !bytes.HasSuffix(b, []byte("#END#")) { return config{}, errors.New("incomplete embedded configuration") }
	var c config
	if err := json.Unmarshal(b[:len(b)-5], &c); err != nil {
		return c, errors.New("invalid embedded configuration") // Do not echo secrets.
	}
	return c, nil
}

func certificatePin(cert *x509.Certificate) string {
	sum := sha256.Sum256(cert.RawSubjectPublicKeyInfo)
	return base64.StdEncoding.EncodeToString(sum[:])
}
func pinnedTLS(pin string) (*tls.Config, error) {
	pin = strings.TrimPrefix(pin, "sha256//")
	decoded, err := base64.StdEncoding.DecodeString(pin)
	if err != nil || len(decoded) != sha256.Size { return nil, errors.New("--pin must be a base64 SHA256 public-key pin") }
	return &tls.Config{
		MinVersion: tls.VersionTLS12,
		// The manager uses a self-signed cert. SPKI pinning replaces CA validation.
		InsecureSkipVerify: true,
		VerifyConnection: func(cs tls.ConnectionState) error {
			if len(cs.PeerCertificates) == 0 || subtle.ConstantTimeCompare([]byte(certificatePin(cs.PeerCertificates[0])), []byte(pin)) != 1 {
				return errors.New("manager TLS public-key pin mismatch")
			}
			return nil
		},
	}, nil
}
func (c config) client() (*http.Client, error) {
	u, err := url.Parse(c.Server)
	if err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") {
		return nil, errors.New("--server must be an HTTPS manager origin")
	}
	if c.Session == "" || strings.ContainsAny(c.Session, "/?#\\") || c.Token == "" { return nil, errors.New("--session and --token are required") }
	t, err := pinnedTLS(c.Pin)
	if err != nil { return nil, err }
	return &http.Client{
		Transport: &http.Transport{TLSClientConfig: t, TLSHandshakeTimeout: 30*time.Second, ResponseHeaderTimeout: 5*time.Minute},
		CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("manager redirects are not allowed") },
	}, nil
}
func send(client *http.Client, c config, method, path string, body io.Reader, compressed bool) error {
	endpoint := strings.TrimRight(c.Server, "/") + "/api/v1/p2v/upload/" + url.PathEscape(c.Session) + path
	req, err := http.NewRequest(method, endpoint, body)
	if err != nil { return errors.New("cannot create upload request") }
	req.Header.Set("X-P2V-Token", c.Token)
	req.Header.Set("Content-Type", "application/json")
	if compressed { req.Header.Set("Content-Encoding", "gzip"); req.Header.Set("Content-Type", "application/octet-stream") }
	resp, err := client.Do(req)
	if err != nil { return fmt.Errorf("manager request failed: %w", err) }
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		// Never print an untrusted response body which might echo credentials.
		return fmt.Errorf("manager rejected %s %s: HTTP %d", method, path, resp.StatusCode)
	}
	_, err = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
	return err
}

func uploadDisk(client *http.Client, c config, d disk, index int, source io.Reader) error {
	reader, writer := io.Pipe()
	done := make(chan error, 1)
	go func() {
		z, err := gzip.NewWriterLevel(writer, gzip.BestSpeed)
		if err == nil {
			chunk := (4 << 20) / int(d.sector) * int(d.sector)
			if chunk == 0 { err = errors.New("unsupported disk sector size") } else {
				buf := make([]byte, chunk)
				start, last := time.Now(), time.Now()
				var copied int64
				for copied < d.Size {
					want := int64(len(buf)); if d.Size-copied < want { want = d.Size-copied }
					var n int
					n, err = io.ReadFull(source, buf[:int(want)])
					if err != nil { err = fmt.Errorf("read %s at byte %d: %w", d.Name, copied, err); break }
					_, err = z.Write(buf[:n]); if err != nil { break }
					copied += int64(n)
					if time.Since(last) >= time.Second || copied == d.Size {
						fmt.Printf("\r%s: %.1f%%  %.1f MiB/s", d.Name, 100*float64(copied)/float64(d.Size), float64(copied)/(1<<20)/time.Since(start).Seconds())
						last = time.Now()
					}
				}
			}
			closeErr := z.Close(); if err == nil { err = closeErr }
		}
		writer.CloseWithError(err)
		done <- err
	}()
	err := send(client, c, http.MethodPut, fmt.Sprintf("/disks/%d", index), reader, true)
	reader.CloseWithError(err) // Unblock compression when HTTP fails early.
	copyErr := <-done
	fmt.Println()
	if err != nil { return err }
	return copyErr
}

func executableConfig() (config, error) {
	name, err := os.Executable(); if err != nil { return config{}, err }
	f, err := os.Open(name); if err != nil { return config{}, err }; defer f.Close()
	info, err := f.Stat(); if err != nil { return config{}, err }
	return readTrailer(f, info.Size())
}
