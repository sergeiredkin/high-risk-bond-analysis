// Document research is served by the same local app origin so the embedded
// UI can use the existing /api/ask and /api/feedback endpoints without CORS.
PAGES.research = {
  render(el) {
    el.innerHTML = `<div class="research-shell">
      <div class="research-status" role="status" aria-live="polite">
        <span data-research-message>Подключаю локальный поиск документов…</span>
        <button type="button" class="pill-outline" data-research-retry hidden>Повторить проверку</button>
      </div>
      <iframe class="research-frame" title="Документальный поиск HyBond" loading="lazy" hidden></iframe>
    </div>`;
    const message = el.querySelector('[data-research-message]');
    const retry = el.querySelector('[data-research-retry]');
    const frame = el.querySelector('.research-frame');

    const checkService = async () => {
      retry.hidden = true;
      message.textContent = 'Проверяю доступность локального RAG-сервиса…';
      try {
        const response = await fetch('/api/health', { cache: 'no-store' });
        const health = response.ok ? await response.json() : null;
        if (!health || health.ok !== true) throw new Error('RAG API недоступен');
        frame.src = '/rag.html';
        frame.hidden = false;
        message.parentElement.hidden = true;
      } catch (_error) {
        frame.hidden = true;
        message.textContent = 'Документальный поиск не подключён. Запустите объединённый HyBond app server и повторите проверку.';
        retry.hidden = false;
      }
    };

    retry.addEventListener('click', checkService);
    checkService();
  },
};
