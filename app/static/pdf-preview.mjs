import * as pdfjs from '/static/pdfjs/pdf.min.mjs';

// All resources and report bytes stay on this application's loopback origin.
pdfjs.GlobalWorkerOptions.workerSrc = '/static/pdfjs/pdf.worker.min.mjs';
const $ = id => document.getElementById(id);
const source = document.querySelector('script[data-pdf-url]').dataset.pdfUrl;
let pdf, pageNumber = 1, generation = 0, resizeTimer;

async function renderPage() {
  const current = ++generation;
  $('previous').disabled = pageNumber <= 1;
  $('next').disabled = pageNumber >= pdf.numPages;
  $('pageLabel').textContent = `第 ${pageNumber} / ${pdf.numPages} 页`;
  $('status').hidden = false;
  try {
    const page = await pdf.getPage(pageNumber);
    const base = page.getViewport({scale: 1});
    const zoom = $('zoom').value;
    const width = Math.max(280, $('pages').clientWidth);
    const scale = zoom === 'fit' ? Math.min(2, width / base.width) : Number(zoom);
    const viewport = page.getViewport({scale});
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const canvas = document.createElement('canvas');
    canvas.width = Math.floor(viewport.width * ratio);
    canvas.height = Math.floor(viewport.height * ratio);
    canvas.style.width = `${Math.floor(viewport.width)}px`;
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', `PDF 报告第 ${pageNumber} 页，可展开本页文字阅读`);
    await page.render({canvasContext: canvas.getContext('2d'), viewport,
      transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0]}).promise;
    const text = await page.getTextContent();
    if (current !== generation) return;
    $('pages').replaceChildren(canvas);
    $('pageText').textContent = text.items.map(item => item.str + (item.hasEOL ? '\n' : ' ')).join('');
    $('status').hidden = true;
    $('error').hidden = true;
  } catch {
    if (current === generation) { $('status').hidden = true; $('error').hidden = false; }
  }
}

$('previous').addEventListener('click', () => { if (pageNumber > 1) { pageNumber--; renderPage(); } });
$('next').addEventListener('click', () => { if (pageNumber < pdf.numPages) { pageNumber++; renderPage(); } });
$('zoom').addEventListener('change', () => { if (pdf) renderPage(); });
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  if (pdf) resizeTimer = setTimeout(renderPage, 150);
});
try {
  pdf = await pdfjs.getDocument({url: source, isEvalSupported: false, useSystemFonts: true,
    disableAutoFetch: true, disableStream: true}).promise;
  await renderPage();
} catch {
  $('status').hidden = true;
  $('pageLabel').textContent = 'PDF 预览未就绪';
  $('error').hidden = false;
}
