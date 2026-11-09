package app

import (
	"encoding/json"
	"errors"
	"fmt"
	"golang.org/x/crypto/bcrypt"
	"os"
	"path/filepath"
	"strings"
)

type Config struct {
	RootPath            string `json:"root_path"`
	SMBRoot             string `json:"smb_root"`
	ScanIntervalMinutes int    `json:"scan_interval_minutes"`
	Port                int    `json:"port"`
	PasswordHash        string `json:"password_hash,omitempty"`
}

func Init(dataDir string, cfg Config, password string) error {
	if _, err := os.Stat(filepath.Join(dataDir, "config.json")); err == nil {
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if len([]rune(password)) < 12 || len(password) > 72 {
		return errors.New("密码至少需要12个字符，且UTF-8编码不超过72字节")
	}
	if cfg.Port < 1024 || cfg.Port > 65535 {
		return errors.New("端口必须在1024–65535之间")
	}
	if !filepath.IsAbs(cfg.RootPath) {
		return errors.New("资料目录必须是绝对路径")
	}
	if cfg.ScanIntervalMinutes == 0 {
		cfg.ScanIntervalMinutes = 15
	}
	if cfg.ScanIntervalMinutes < 1 || cfg.ScanIntervalMinutes > 1440 {
		return errors.New("更新间隔须为1–1440分钟")
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	if err != nil {
		return err
	}
	cfg.PasswordHash = string(hash)
	if err = os.MkdirAll(dataDir, 0700); err != nil {
		return err
	}
	if err = os.Chmod(dataDir, 0700); err != nil {
		return err
	}
	return SaveConfig(dataDir, cfg)
}

func LoadConfig(dataDir string) (Config, error) {
	var cfg Config
	b, err := os.ReadFile(filepath.Join(dataDir, "config.json"))
	if err != nil {
		return cfg, fmt.Errorf("请先完成初始化：%w", err)
	}
	if err = json.Unmarshal(b, &cfg); err != nil {
		return cfg, err
	}
	if cfg.Port < 1024 || cfg.Port > 65535 || cfg.ScanIntervalMinutes < 1 || cfg.ScanIntervalMinutes > 1440 || cfg.PasswordHash == "" {
		return cfg, errors.New("配置无效")
	}
	if !filepath.IsAbs(cfg.RootPath) {
		return cfg, errors.New("资料目录必须为绝对路径")
	}
	return cfg, nil
}

func SaveConfig(dataDir string, cfg Config) error {
	b, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dataDir, "config-*.tmp")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name)
	if err = tmp.Chmod(0600); err != nil {
		tmp.Close()
		return err
	}
	if _, err = tmp.Write(b); err == nil {
		err = tmp.Sync()
	}
	cerr := tmp.Close()
	if err == nil {
		err = cerr
	}
	if err != nil {
		return err
	}
	return os.Rename(name, filepath.Join(dataDir, "config.json"))
}

func inside(base, path string) bool {
	rel, err := filepath.Rel(base, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) && !filepath.IsAbs(rel)
}

func validateRoot(path, dataDir string) error {
	if !filepath.IsAbs(path) {
		return errors.New("请输入绝对目录路径")
	}
	real, err := filepath.EvalSymlinks(path)
	if err != nil {
		return fmt.Errorf("无法读取目录，请检查路径和权限：%w", err)
	}
	dataReal, err := filepath.EvalSymlinks(dataDir)
	if err != nil {
		return err
	}
	if inside(real, dataReal) || inside(dataReal, real) {
		return errors.New("资料目录与 SynoFind 数据目录不能相互包含")
	}
	f, err := os.Open(real)
	if err != nil {
		return fmt.Errorf("没有目录读取权限：%w", err)
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return err
	}
	if !info.IsDir() {
		return errors.New("请选择文件夹")
	}
	_, err = f.Readdirnames(-1)
	if err != nil {
		return fmt.Errorf("无法列出目录，请授予套件只读权限：%w", err)
	}
	return nil
}
