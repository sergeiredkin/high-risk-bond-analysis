// Pages not rebuilt yet (redesign stages 2 and 3). Each keeps the page
// container and title the reference uses, and says plainly that nothing is
// built -- no placeholder numbers. Replace an entry with a real page file.

function pendingPage(container, { title, stage, heading, body, items }) {
  return {
    render(el) {
      el.innerHTML = `<div class="${container}">
        ${title ? `<div class="page-title-row"><div class="page-title">${escapeHtml(title)}</div></div>` : ''}
        <div class="pending">
          <div class="pending-kicker">Ещё не собрано · этап ${stage} из 3</div>
          <div class="pending-title">${escapeHtml(heading)}</div>
          <div class="pending-body">${escapeHtml(body)}</div>
          <ul class="pending-list">${items.map(i => `<li>${escapeHtml(i)}</li>`).join('')}</ul>
          <div class="pending-actions">
            <button type="button" class="pill-outline" data-page="overview">Вернуться к обзору</button>
          </div>
        </div>
      </div>`;
    },
  };
}

PAGES.terminal = pendingPage('page-terminal', {
  title: null, stage: 2, // reference terminal page has no title row
  heading: 'Карта и скринер пока не перенесены в новый дизайн',
  body: 'Страница появится на втором этапе. Данных здесь нет намеренно — никаких заглушек вместо реальных бумаг.',
  items: [
    'карта спредов на D3 с выделением области рамкой',
    'скринер, который фильтруется по выделению на карте',
    'отметка бумаг для наблюдения прямо в строке скринера',
    'разбор бумаги в боковой панели',
  ],
});

PAGES.portfolio = pendingPage('page-wide', {
  title: 'Портфель', stage: 3,
  heading: 'Портфель пока не перенесён в новый дизайн',
  body: 'Страница появится на третьем этапе, вместе с отказом от отдельного вотчлиста.',
  items: [
    'пресеты портфеля и сборка из выделения на карте',
    'трекер ставки ЦБ',
    'проверка портфеля по ограничениям',
  ],
});

PAGES.data = pendingPage('page-wide', {
  title: null, stage: 3, // reference data page has no title row
  heading: 'Данные пока не перенесены в новый дизайн',
  body: 'Страница появится на третьем этапе. Метка здоровья конвейера в шапке уже считается по реальной выгрузке.',
  items: [
    'здоровье конвейера по кодам',
    'источники и их свежесть',
    'лента событий с классификацией',
  ],
});
