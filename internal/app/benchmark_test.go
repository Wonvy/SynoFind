package app

import (
	"fmt"
	"strings"
	"testing"
)

// This benchmark measures the actual SQLite query path, including result count,
// filtering, sorting and first-page decoding. It creates no source documents.
func BenchmarkSearch100K(b *testing.B) {
	a := newTestApp(b)
	tx, err := a.db.Begin()
	if err != nil {
		b.Fatal(err)
	}
	stmt, err := tx.Prepare("INSERT INTO files(id,path,name,path_fold,size,mtime,extension) VALUES(?,?,?,?,?,?,?)")
	if err != nil {
		tx.Rollback()
		b.Fatal(err)
	}
	for i := 0; i < 100000; i++ {
		name := fmt.Sprintf("报表_%06d_合同完成.pdf", i)
		path := fmt.Sprintf("部门%02d/项目%04d/%s", i%97, i%1000, name)
		if _, err := stmt.Exec(fmt.Sprintf("%032x", i), path, name, strings.ToLower(path), int64(i+100), int64(1767225600000000000+i), "pdf"); err != nil {
			stmt.Close()
			tx.Rollback()
			b.Fatal(err)
		}
	}
	stmt.Close()
	if err := tx.Commit(); err != nil {
		b.Fatal(err)
	}
	for _, tc := range []struct {
		name, query string
		total       int
	}{
		{"selective_trigram", "报表_042", 1000},
		{"two_chinese_characters", "合同", 100000},
		{"browse_all", "", 100000},
	} {
		b.Run(tc.name, func(b *testing.B) {
			o := SearchOptions{Query: tc.query, Page: 1, Limit: 50, Sort: "name"}
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				r, err := a.Search(o)
				if err != nil {
					b.Fatal(err)
				}
				if r.Total != tc.total || len(r.Items) != 50 {
					b.Fatalf("incorrect benchmark result: total=%d rows=%d", r.Total, len(r.Items))
				}
			}
		})
	}
}
