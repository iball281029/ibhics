package main

import (
    "bytes"
    "encoding/json"
    "io"
    "net/http"
    "net/http/httptest"
    "strings"
    "testing"
)

func TestDeltaHashesTailAndShortRead(t *testing.T) {
    data := bytes.Repeat([]byte("a"), deltaChunk+512)
    values, err := deltaHashes(bytes.NewReader(data), int64(len(data)))
    if err != nil || len(values) != 2 || values[0] == values[1] { t.Fatal(values, err) }
    if _, err := deltaHashes(bytes.NewReader(data), int64(len(data)+1)); err == nil { t.Fatal("accepted short source") }
}

func TestDeltaRound(t *testing.T) {
    for _, mode := range []string{"ok", "bad-index", "reject", "changed", "unchanged"} {
        t.Run(mode, func(t *testing.T) {
            data := bytes.Repeat([]byte("a"), deltaChunk+512)
            source := bytes.NewReader(data)
            committed, uploads := false, 0
            server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
                if r.Header.Get("X-P2V-Token") != "private" { t.Error("missing token") }
                if strings.HasSuffix(r.URL.Path, "/begin") {
                    var body struct { Hashes [][]string `json:"hashes"` }
                    if err := json.NewDecoder(r.Body).Decode(&body); err != nil || len(body.Hashes) != 1 || len(body.Hashes[0]) != 2 { t.Error("invalid hashes") }
                    needed := [][]int{{1}}
                    if mode == "bad-index" { needed[0] = []int{9} }
                    if mode == "unchanged" { needed[0] = []int{} }
                    if mode == "changed" { data[0] = 'b' }
                    json.NewEncoder(w).Encode(deltaPlan{strings.Repeat("a",32), deltaChunk, needed})
                } else if r.Method == "PUT" {
                    uploads++
                    got, _ := io.ReadAll(r.Body)
                    if !bytes.Equal(got, data[deltaChunk:]) { t.Error("wrong requested chunk") }
                    if mode == "reject" { w.WriteHeader(422) }
                } else if strings.HasSuffix(r.URL.Path, "/commit") { committed = true
                } else { t.Error("unexpected request") }
            }))
            defer server.Close()
            machine := inventory{Disks: []disk{{Name:"PhysicalDrive0", Size:int64(len(data))}}}
            err := replicateDisks(server.Client(), config{Server:server.URL, Session:"session", Token:"private"}, machine, []io.ReaderAt{source})
            success := mode == "ok" || mode == "unchanged"
            if (err == nil) != success || committed != success { t.Fatalf("err=%v committed=%v", err, committed) }
            if mode == "unchanged" && uploads != 0 { t.Fatal("uploaded unchanged chunks") }
        })
    }
}
