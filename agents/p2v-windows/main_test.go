package main

import (
	"bytes"
	"compress/gzip"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestReadTrailer(t *testing.T) {
	expected := config{Server:"https://manager:8888", Session:"abc123", Token:"private", Pin:"pin"}
	body, _ := json.Marshal(expected)
	data := append([]byte("MZ executable data"+trailerMarker), body...)
	data = append(data, []byte("#END#")...)
	got, err := readTrailer(bytes.NewReader(data), int64(len(data)))
	if err != nil || got != expected { t.Fatalf("configuration did not round trip: %v", err) }
	for _, input := range []string{"MZ", "", "MZ#IBHICS-P2V#{}#END#"} {
		_, err = readTrailer(strings.NewReader(input), int64(len(input)))
		if !errors.Is(err, errNoTrailer) { t.Fatal("expected missing trailer") }
	}
	for _, input := range []string{trailerMarker+"{}", trailerMarker+"bad#END#", trailerMarker+"{}#END#extra"} {
		if _, err = readTrailer(strings.NewReader(input), int64(len(input))); err == nil || errors.Is(err, errNoTrailer) { t.Fatal("expected invalid trailer") }
	}
}
func TestPinAndTLS(t *testing.T) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader); if err != nil { t.Fatal(err) }
	template := &x509.Certificate{SerialNumber:big.NewInt(1), NotBefore:time.Now(), NotAfter:time.Now().Add(time.Hour)}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key); if err != nil { t.Fatal(err) }
	cert, err := x509.ParseCertificate(der); if err != nil { t.Fatal(err) }
	spki, _ := x509.MarshalPKIXPublicKey(&key.PublicKey)
	digest := sha256.Sum256(spki)
	expected := base64.StdEncoding.EncodeToString(digest[:])
	if certificatePin(cert) != expected { t.Fatal("pin must hash SPKI, not whole certificate") }
	cfg, err := pinnedTLS("sha256//"+expected); if err != nil { t.Fatal(err) }
	if err = cfg.VerifyConnection(tls.ConnectionState{PeerCertificates:[]*x509.Certificate{cert}}); err != nil { t.Fatal(err) }
	if cfg.VerifyConnection(tls.ConnectionState{}) == nil { t.Fatal("accepted missing certificate") }
	cert.RawSubjectPublicKeyInfo = []byte("wrong key")
	if cfg.VerifyConnection(tls.ConnectionState{PeerCertificates:[]*x509.Certificate{cert}}) == nil { t.Fatal("accepted wrong key") }
	for _, pin := range []string{"", "bad", base64.StdEncoding.EncodeToString([]byte("short"))} {
		if _, err = pinnedTLS(pin); err == nil { t.Fatal("accepted malformed pin") }
	}
}
func TestInventoryJSON(t *testing.T) {
	m := inventory{Hostname:"server", CPUs:8, MemoryMB:8192, Firmware:"uefi", OS:"Windows Server 2022", MACs:[]string{"00:11:22:33:44:55"}, Disks:[]disk{{Name:"PhysicalDrive0", Size:1<<30, sector:4096}}}
	b, err := json.Marshal(m); if err != nil { t.Fatal(err) }
	var decoded map[string]any
	if err = json.Unmarshal(b, &decoded); err != nil { t.Fatal(err) }
	if decoded["hostname"] != "server" || decoded["cpus"] != float64(8) || decoded["memory_mb"] != float64(8192) || decoded["firmware"] != "uefi" || decoded["os"] != "Windows Server 2022" { t.Fatal("inventory JSON contract changed") }
	disks := decoded["disks"].([]any)
	d := disks[0].(map[string]any)
	if d["name"] != "PhysicalDrive0" || d["size_bytes"] != float64(1<<30) || len(d) != 2 { t.Fatal("disk JSON contract changed") }
}
func TestUploadStreaming(t *testing.T) {
	raw := bytes.Repeat([]byte("disk sector data"), 1024)
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-P2V-Token") != "private" || r.Header.Get("Content-Encoding") != "gzip" { t.Error("missing upload headers") }
		z, err := gzip.NewReader(r.Body); if err != nil { t.Error(err); w.WriteHeader(400); return }; defer z.Close()
		got, err := io.ReadAll(z)
		if err != nil || !bytes.Equal(got, raw) { t.Error("disk stream corrupted") }
	}))
	defer server.Close()
	c := config{Server:server.URL, Session:"abc", Token:"private", Pin:certificatePin(server.Certificate())}
	client, err := c.client(); if err != nil { t.Fatal(err) }
	if err = uploadDisk(client, c, disk{Name:"PhysicalDrive0", Size:int64(len(raw)), sector:512}, 0, bytes.NewReader(raw)); err != nil { t.Fatal(err) }
	// Early server rejection must unblock the compression goroutine.
	reject := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(403) }))
	defer reject.Close()
	c.Server = reject.URL; c.Pin = certificatePin(reject.Certificate()); client, _ = c.client()
	if uploadDisk(client, c, disk{Name:"PhysicalDrive0", Size:512, sector:512}, 0, bytes.NewReader(make([]byte,512))) == nil { t.Fatal("accepted rejected upload") }
}
