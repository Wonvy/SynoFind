export const PDF_MAX_CANVAS_PIXELS = 8_000_000;

/** Keep a single page canvas within a fixed pixel budget, including long pages. */
export function fitPDFPage(pageWidth: number, pageHeight: number, availableWidth: number, devicePixelRatio: number) {
  if (![pageWidth, pageHeight, availableWidth].every(value => Number.isFinite(value) && value > 0)) {
    throw new Error('PDF 页面尺寸无效。');
  }
  const scale = Math.max(1, Math.floor(availableWidth)) / pageWidth;
  const width = pageWidth * scale;
  const height = pageHeight * scale;
  const ratio = Number.isFinite(devicePixelRatio) ? Math.max(1, Math.min(2, devicePixelRatio)) : 1;
  const outputScale = Math.min(ratio, Math.sqrt(PDF_MAX_CANVAS_PIXELS / (width * height)));
  return {
    scale, width, height, outputScale,
    pixelWidth: Math.max(1, Math.floor(width * outputScale)),
    pixelHeight: Math.max(1, Math.floor(height * outputScale)),
  };
}

export function pdfErrorMessage(reason: unknown): string {
  const error = reason as { name?: string; status?: number; message?: string } | undefined;
  if (error?.name === 'PasswordException') return '这是加密 PDF，请下载后输入密码查看。';
  if (error?.name === 'InvalidPDFException') return 'PDF 文件格式无效或已损坏，请下载后检查。';
  if (error?.status === 401) return '登录已过期，请重新登录后预览。';
  if (error?.status === 403) return '没有读取此文件的权限。';
  if (error?.status === 404) return '文件已移动或删除，请刷新索引。';
  if (error?.name === 'ResponseException' || error instanceof TypeError) return '无法读取 PDF，请检查网络连接或下载后查看。';
  return 'PDF 预览未能完成。可以重试，或下载后查看。';
}
