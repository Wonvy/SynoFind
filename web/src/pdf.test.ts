import { describe, expect, it } from 'vitest';
import { fitPDFPage, PDF_MAX_CANVAS_PIXELS, pdfErrorMessage } from './pdf';

describe('PDF page fitting', () => {
  it('fits the available width and caps device pixel ratio', () => {
    const result = fitPDFPage(600, 900, 900, 3);
    expect(result.width).toBe(900);
    expect(result.height).toBe(1350);
    expect(result.outputScale).toBe(2);
    expect(result.pixelWidth).toBe(1800);
  });
  it('bounds memory use for exceptionally tall pages', () => {
    const result = fitPDFPage(600, 100000, 1000, 2);
    expect(result.pixelWidth * result.pixelHeight).toBeLessThanOrEqual(PDF_MAX_CANVAS_PIXELS);
    expect(result.outputScale).toBeLessThan(1);
  });
  it('rejects invalid dimensions without allocating a canvas', () => {
    expect(() => fitPDFPage(0, 900, 800, 1)).toThrow();
    expect(() => fitPDFPage(600, Infinity, 800, 1)).toThrow();
    expect(() => fitPDFPage(600, 900, -1, 1)).toThrow();
  });
});

describe('PDF failure messages', () => {
  it('explains encrypted, missing and unauthenticated files', () => {
    expect(pdfErrorMessage({ name: 'PasswordException' })).toContain('加密');
    expect(pdfErrorMessage({ status: 404 })).toContain('移动或删除');
    expect(pdfErrorMessage({ status: 401 })).toContain('登录已过期');
  });
  it('does not expose raw parser errors or document content', () => {
    expect(pdfErrorMessage(new Error('private document data'))).not.toContain('private');
  });
});
