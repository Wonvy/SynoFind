import { describe, expect, it } from 'vitest';
import { EMPTY_FILTERS, formatBytes, searchQuery } from './format';

describe('search query', () => {
  it('preserves literal Unicode and special characters in the query', () => {
    const params = new URLSearchParams(searchQuery(' 合同 & 50%_ ', EMPTY_FILTERS, 2, 'size', 'desc'));
    expect(params.get('q')).toBe('合同 & 50%_');
    expect(params.get('page')).toBe('2');
    expect(params.get('order')).toBe('desc');
  });
  it('converts sizes and normalizes extensions', () => {
    const params = new URLSearchParams(searchQuery('', { ...EMPTY_FILTERS, minSize: '1.5', maxSize: '2', ext: '.pdf, txt' }, 1, 'name', 'asc'));
    expect(params.get('min_size')).toBe('1572864');
    expect(params.get('max_size')).toBe('2097152');
    expect(params.get('ext')).toBe('pdf,txt');
  });
  it('rejects inverted and invalid size ranges', () => {
    expect(() => searchQuery('', { ...EMPTY_FILTERS, minSize: '2', maxSize: '1' }, 1, 'name', 'asc')).toThrow();
    expect(() => searchQuery('', { ...EMPTY_FILTERS, minSize: 'Infinity' }, 1, 'name', 'asc')).toThrow();
    expect(() => searchQuery('', { ...EMPTY_FILTERS, maxSize: '-1' }, 1, 'name', 'asc')).toThrow();
  });
  it('covers the complete local date and rejects an inverted date range', () => {
    const params = new URLSearchParams(searchQuery('', { ...EMPTY_FILTERS, after: '2026-11-09', before: '2026-11-09' }, 1, 'name', 'asc'));
    expect(new Date(params.get('modified_after')!).getHours()).toBe(0);
    expect(new Date(params.get('modified_before')!).getHours()).toBe(23);
    expect(() => searchQuery('', { ...EMPTY_FILTERS, after: '2026-11-10', before: '2026-11-09' }, 1, 'name', 'asc')).toThrow();
  });
});

describe('formatBytes', () => {
  it('handles empty files, unit boundaries and invalid data', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(1024)).toBe('1 KB');
    expect(formatBytes(1536)).toBe('1.5 KB');
    expect(formatBytes(-1)).toBe('—');
  });
});
