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
	DSMURL              string `json:"dsm_url"`
	DSMUsername         string `json:"dsm_username"`
	DSMPassword         string `json:"dsm_password,omitempty"`
	ScanIntervalMinutes int    `json:"-"`
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
	if cfg.RootPath == "" {
		cfg.RootPath = "/Wonvy"
	}
	cfg.RootPath = normalizeFileStationRoot(cfg.RootPath)
	if err := validateFileStationRoot(cfg.RootPath); err != nil {
		return err
	}
	if cfg.DSMURL == "" {
		cfg.DSMURL = "http://127.0.0.1:5000"
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
	if cfg.Port < 1024 || cfg.Port > 65535 || cfg.PasswordHash == "" {
		return cfg, errors.New("配置无效")
	}
	// Older suite versions stored the NAS filesystem path. File Station uses
	// shared-folder paths, so migrate /volumeN/share/... to /share/....
	cfg.RootPath = normalizeFileStationRoot(cfg.RootPath)
	if err = validateFileStationRoot(cfg.RootPath); err != nil {
		return cfg, err
	}
	if cfg.DSMURL == "" {
		cfg.DSMURL = "http://127.0.0.1:5000"
	}
	// The old installer used this example UNC path as its default. Clear it so
	// new DSM API results do not expose a path that points at another NAS.
	if strings.EqualFold(strings.TrimSpace(cfg.SMBRoot), `\\10.0.0.10\Wonvy`) {
		cfg.SMBRoot = ""
	}
	return cfg, nil
}

func normalizeFileStationRoot(root string) string {
	root = strings.TrimSpace(strings.ReplaceAll(root, `\`, "/"))
	if strings.HasPrefix(root, "/volume") {
		parts := strings.Split(strings.TrimPrefix(root, "/"), "/")
		if len(parts) >= 2 && len(parts[0]) > len("volume") && strings.HasPrefix(parts[0], "volume") {
			volume := strings.TrimPrefix(parts[0], "volume")
			if volume != "" {
				allDigits := true
				for _, char := range volume {
					if char < '0' || char > '9' {
						allDigits = false
						break
					}
				}
				if allDigits {
					return "/" + strings.Join(parts[1:], "/")
				}
			}
		}
	}
	return root
}

func validateFileStationRoot(root string) error {
	if !strings.HasPrefix(root, "/") || root == "/" || strings.Contains(root, `\`) || strings.Contains(root, "//") {
		return errors.New("资料目录须使用 File Station 路径，例如 /共享文件夹")
	}
	for _, part := range strings.Split(strings.TrimPrefix(root, "/"), "/") {
		if part == "" || part == "." || part == ".." {
			return errors.New("File Station 路径不能包含空目录或相对路径")
		}
		for _, char := range part {
			if char < 0x20 || char == 0x7f {
				return errors.New("File Station 路径不能包含控制字符")
			}
		}
	}
	return nil
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
