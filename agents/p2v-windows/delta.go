package main

import (
    "bytes"
    "crypto/sha256"
    "encoding/hex"
    "encoding/json"
    "errors"
    "fmt"
    "io"
    "net/http"
    "net/url"
    "strings"
)

const deltaChunk = 4 << 20

type deltaPlan struct {
    Round string `json:"round"`
    ChunkSize int `json:"chunk_size"`
    Needed [][]int `json:"needed"`
}

func deltaRequest(client *http.Client, c config, method, path string, body []byte, result interface{}) error {
    endpoint := strings.TrimRight(c.Server, "/") + "/api/v1/p2v/upload/" + url.PathEscape(c.Session) + "/delta" + path
    req, err := http.NewRequest(method, endpoint, bytes.NewReader(body))
    if err != nil { return errors.New("cannot create delta request") }
    req.Header.Set("X-P2V-Token", c.Token)
    req.Header.Set("Content-Type", "application/json")
    resp, err := client.Do(req)
    if err != nil { return errors.New("delta manager request failed") }
    defer resp.Body.Close()
    if resp.StatusCode < 200 || resp.StatusCode >= 300 {
        return fmt.Errorf("manager rejected delta request: HTTP %d", resp.StatusCode)
    }
    if result == nil { _, err = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20)); return err }
    if err = json.NewDecoder(io.LimitReader(resp.Body, 64<<20)).Decode(result); err != nil {
        return errors.New("invalid manager delta response")
    }
    return nil
}

func readDeltaChunk(source io.ReaderAt, size int64, index int) ([]byte, error) {
    offset := int64(index) * deltaChunk
    if index < 0 || offset < 0 || offset >= size { return nil, errors.New("invalid chunk index") }
    length := int64(deltaChunk)
    if size-offset < length { length = size-offset }
    data := make([]byte, int(length))
    n, err := source.ReadAt(data, offset)
    if n != len(data) { return nil, errors.New("short source disk read") }
    if err != nil && err != io.EOF { return nil, errors.New("source disk read failed") }
    return data, nil
}
func deltaHashes(source io.ReaderAt, size int64) ([]string, error) {
    if size <= 0 { return nil, errors.New("invalid disk size") }
    hashes := make([]string, 0, (size+deltaChunk-1)/deltaChunk)
    for i := 0; int64(i)*deltaChunk < size; i++ {
        data, err := readDeltaChunk(source, size, i)
        if err != nil { return nil, err }
        digest := sha256.Sum256(data)
        hashes = append(hashes, hex.EncodeToString(digest[:]))
    }
    return hashes, nil
}

// Caller owns snapshot lifetime. Without VSS these readers are live disks; a second
// hash pass detects churn, but does not provide application consistency.
func replicateDisks(client *http.Client, c config, machine inventory, sources []io.ReaderAt) error {
    if len(sources) != len(machine.Disks) || len(sources) == 0 { return errors.New("disk reader count mismatch") }
    hashes := make([][]string, len(sources))
    for i, source := range sources {
        var err error
        hashes[i], err = deltaHashes(source, machine.Disks[i].Size)
        if err != nil { return err }
    }
    body, err := json.Marshal(struct {
        Machine inventory `json:"machine"`
        Hashes [][]string `json:"hashes"`
    }{machine, hashes})
    if err != nil { return err }
    var plan deltaPlan
    if err = deltaRequest(client, c, "POST", "/begin", body, &plan); err != nil { return err }
    id, idErr := hex.DecodeString(plan.Round)
    if idErr != nil || len(id) != 16 || plan.ChunkSize != deltaChunk || len(plan.Needed) != len(sources) {
        return errors.New("invalid delta plan")
    }
    for i, chunks := range plan.Needed {
        for _, chunk := range chunks {
            if chunk < 0 || chunk >= len(hashes[i]) { return errors.New("invalid requested chunk") }
            data, err := readDeltaChunk(sources[i], machine.Disks[i].Size, chunk)
            if err != nil { return err }
            digest := sha256.Sum256(data)
            if hex.EncodeToString(digest[:]) != hashes[i][chunk] { return errors.New("source changed; quiesce writers and retry") }
            path := fmt.Sprintf("/%s/disks/%d/chunks/%d", plan.Round, i, chunk)
            if err = deltaRequest(client, c, "PUT", path, data, nil); err != nil { return err }
        }
    }
    for i, source := range sources {
        current, err := deltaHashes(source, machine.Disks[i].Size)
        if err != nil { return err }
        for n, digest := range current {
            if digest != hashes[i][n] { return errors.New("source changed during round; no commit") }
        }
    }
    return deltaRequest(client, c, "POST", "/"+plan.Round+"/commit", nil, nil)
}
