package main

import (
	"bufio"
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"synofind/internal/app"
	"synofind/internal/webui"
	"syscall"
	"time"
)

var version = "dev"

func main() {
	if err := run(); err != nil {
		log.Print(err)
		os.Exit(1)
	}
}
func run() error {
	if len(os.Args) < 2 {
		return fmt.Errorf("用法: synofind init|serve|version")
	}
	switch os.Args[1] {
	case "version":
		fmt.Println(version)
		return nil
	case "init":
		f := flag.NewFlagSet("init", flag.ContinueOnError)
		data := f.String("data-dir", "", "私有配置目录")
		port := f.Int("port", 8787, "端口")
		root := f.String("root", "/Wonvy", "File Station 共享文件夹路径")
		smb := f.String("smb-root", "", "Windows共享路径（可选）")
		if err := f.Parse(os.Args[2:]); err != nil {
			return err
		}
		if *data == "" {
			return fmt.Errorf("需要 --data-dir")
		}
		password, err := bufio.NewReaderSize(os.Stdin, 4096).ReadString('\n')
		if err != nil && password == "" {
			return fmt.Errorf("请通过标准输入提供初始密码")
		}
		return app.Init(*data, app.Config{RootPath: *root, SMBRoot: *smb, Port: *port}, strings.TrimRight(password, "\r\n"))
	case "serve":
		f := flag.NewFlagSet("serve", flag.ContinueOnError)
		data := f.String("data-dir", "", "私有配置目录")
		listen := f.String("listen", "", "监听地址（默认配置端口）")
		if err := f.Parse(os.Args[2:]); err != nil {
			return err
		}
		if *data == "" {
			return fmt.Errorf("需要 --data-dir")
		}
		a, err := app.New(*data, webui.FS())
		if err != nil {
			return err
		}
		defer a.Close()
		address := *listen
		if address == "" {
			address = fmt.Sprintf(":%d", a.Config().Port)
		}
		server := &http.Server{Addr: address, Handler: a.Handler(), ReadHeaderTimeout: 10 * time.Second, ReadTimeout: 30 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16384}
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		done := make(chan error, 1)
		go func() { log.Printf("SynoFind %s listening on %s", version, address); done <- server.ListenAndServe() }()
		a.Start()
		select {
		case err = <-done:
			if err != nil && err != http.ErrServerClosed {
				return err
			}
			return nil
		case <-ctx.Done():
			shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			err = server.Shutdown(shutdown)
			if err != nil {
				server.Close()
			}
			return err
		}
	default:
		return fmt.Errorf("未知命令：%s", os.Args[1])
	}
}
