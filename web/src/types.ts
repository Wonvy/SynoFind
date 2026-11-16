export interface FileItem {
  id: string;
  name: string;
  path: string;
  size: number;
  modified_at: string;
  extension: string;
  preview_type: '' | 'image' | 'pdf' | 'text' | 'psd' | 'pptx' | 'illustrator';
  smb_path: string;
}

export interface SearchResult {
  items: FileItem[];
  total: number;
  page: number;
  limit: number;
}

export interface IndexStatus {
	connected: boolean;
	last_search_at: string;
	last_error: string;
	root_path: string;
}

export interface Settings {
  root_path: string;
  smb_root: string;
	dsm_url: string;
	dsm_username: string;
	has_dsm_password: boolean;
  port: number;
}

export interface Token {
  id: string;
  name: string;
  created_at: string;
  last_used_at: string;
}

export interface Session { authenticated: boolean; csrf_token: string }
export interface TokenCreated { id: string; name: string; token: string; created_at: string }
export type Page = 'search' | 'index' | 'settings';
export type Notify = (message: string, kind?: 'success' | 'error') => void;
