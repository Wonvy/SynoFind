package app

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"
)

func TestSearchChineseLiteralPathAndExtensionMatching(t *testing.T) {
	a := newTestApp(t)
	fixtures := map[string]string{
		"docs/合同2026最终版.PDF":        "signed contract",
		"docs/合同草案.pdf":             "draft",
		"财务/年度报告2026.xlsx":          "report",
		"special/100%_ready[1].txt": "literal",
		"Code/HelloWorld.GO":        "package main",
	}
	for name, contents := range fixtures {
		writeFixture(t, a.Config().RootPath, name, contents)
	}
	if err := a.scan(context.Background(), a.Config()); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		q    string
		want int
	}{
		{"合", 2}, {"合同", 2}, {"合同2", 1}, {"DOCS 合同", 2}, {"helloWORLD", 1},
		{"年度报告", 1}, {"%_", 1}, {"100%_", 1}, {"[1]", 1}, {"docs 合同 草案", 1},
		{"' OR 1=1 --", 0}, {`"`, 0}, {"does-not-exist", 0},
	} {
		t.Run(tc.q, func(t *testing.T) {
			r := queryAll(t, a, tc.q)
			if r.Total != tc.want {
				t.Fatalf("query %q: got %d, want %d: %+v", tc.q, r.Total, tc.want, r.Items)
			}
		})
	}
	r, err := a.Search(SearchOptions{Ext: ".PDF, GO", Page: 1, Limit: 50})
	if err != nil {
		t.Fatal(err)
	}
	if r.Total != 3 {
		t.Fatalf("extension filter returned %d files", r.Total)
	}
	f := queryAll(t, a, "合同2026").Items[0]
	if f.SMBPath != `\\nas\Wonvy\docs\合同2026最终版.PDF` || f.PreviewType != "pdf" || f.Extension != "pdf" {
		t.Fatalf("incorrect file metadata: %+v", f)
	}
}

func TestSearchDateSizeSortAndPagination(t *testing.T) {
	a := newTestApp(t)
	for i, name := range []string{"a.txt", "b.txt", "c.txt"} {
		p := writeFixture(t, a.Config().RootPath, name, strings.Repeat("x", (i+1)*10))
		ts := time.Date(2026, time.January, i+1, 12, 0, 0, 0, time.UTC)
		if err := os.Chtimes(p, ts, ts); err != nil {
			t.Fatal(err)
		}
	}
	if err := a.scan(context.Background(), a.Config()); err != nil {
		t.Fatal(err)
	}
	min, max := int64(15), int64(25)
	r, err := a.Search(SearchOptions{MinSize: &min, MaxSize: &max, After: "2026-01-02T00:00:00Z", Before: "2026-01-02T23:59:59Z", Page: 1, Limit: 50})
	if err != nil {
		t.Fatal(err)
	}
	if r.Total != 1 || r.Items[0].Name != "b.txt" {
		t.Fatalf("incorrect combined filters: %+v", r)
	}
	r, err = a.Search(SearchOptions{Sort: "size", Order: "desc", Page: 2, Limit: 1})
	if err != nil {
		t.Fatal(err)
	}
	if r.Total != 3 || len(r.Items) != 1 || r.Items[0].Name != "b.txt" {
		t.Fatalf("incorrect pagination: %+v", r)
	}
	r, err = a.Search(SearchOptions{Sort: "modified", Order: "desc", Page: 1, Limit: 1})
	if err != nil || len(r.Items) != 1 || r.Items[0].Name != "c.txt" {
		t.Fatalf("incorrect modified sort: %+v %v", r, err)
	}
	r = queryAll(t, a, "not-present")
	if r.Items == nil {
		t.Fatal("empty results must encode as [] rather than null")
	}
	if _, err := a.Search(SearchOptions{After: "invalid-date", Page: 1, Limit: 50}); err == nil {
		t.Fatal("invalid date accepted")
	}
}
