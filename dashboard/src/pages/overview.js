// Обзор -- hero, the 18 capability blocks and the value band, copy taken
// verbatim from bond-lab-design-reference.html (renderVals() `cap` array and
// the isOverview markup). Deviations from the reference copy, all of them
// counts that had no source in the codebase or were wrong:
//   #6  "трёх агентств" -> "четырёх агентств": db._AGENCY_PRIORITY ranks
//       АКРА, Эксперт РА, НКР, НРА (the block's own body lists all four).
//   #11 "— 9 кодов" dropped: no code registry; codes are logged ad hoc.
//   #14 "834 из 834 тестов" dropped: a test count is not dashboard content.
//   #15 "Семь источников" dropped: not a fixed, sourced number.
//   #17 "27 команд CLI" -> 37: top-level subcommands in `hybond --help`
//       (src/hybond/__main__.py). Update if commands are added or removed.
// The reference's "Мобильная версия" link points at a Claude Design canvas
// file that does not exist in this project, so it is not rendered.

const OVERVIEW_CAPABILITIES = [
  { title: 'Аналитическое ядро', items: [
    ['AEY — доходность после кредитных потерь', 'YTM минус ожидаемые потери (PD × LGD), а не сырая доходность. Ранжирование строится по AEY, поэтому дорогая по купону бумага с плохим балансом опускается вниз.'],
    ['IRR с учётом выживаемости', 'Вероятность дефолта экстраполируется на несколько лет вперёд, а не берётся плоской однолетней. Денежный поток взвешивается кривой выживания эмитента.'],
    ['Скоринг РСБУ по шести опорам', 'Ликвидность, долговая нагрузка, покрытие процентов, рентабельность, денежный поток, структура капитала. Правила в коде и под тестами — не чёрный ящик.'],
    ['Поиск mispricing по G-спреду', 'Рыночный спред к кривой ОФЗ сравнивается со справедливым по модели. Разрыв — это и есть кандидат на покупку или на выход.'],
    ['Carry и roll-down по ОФЗ', 'Отдельно считается, сколько приносит удержание и сколько — сползание по кривой при неизменной форме.'],
    ['Разрешение рейтингов четырёх агентств', 'АКРА > Эксперт РА > НКР > НРА > ручная правка. Один ответ вместо нескольких противоречащих, с датой и приоритетом.'],
    ['Классификация событий локальной моделью', 'Ollama на своей машине: ни один клиентский документ не уходит наружу. Новость получает класс и уверенность.'],
    ['Трекер решений ЦБ', 'Траектория ставки, календарь заседаний и то, что из этого уже заложено в кривую.'],
  ] },
  { title: 'Портфельная логика', items: [
    ['Алгоритмические пресеты', 'Портфели по уровням риска собираются из всей вселенной бумаг по правилам, а не подбираются вручную.'],
    ['Сборка из выделения', 'Любая отфильтрованная или выделенная на карте подборка превращается в отслеживаемый портфель одним действием.'],
  ] },
  { title: 'Целостность данных', items: [
    ['Мониторинг конвейера', 'Плохие данные ловятся до того, как попадут в анализ. Красный код блокирует обновление аналитического слоя.'],
    ['База подтверждённых дефолтов', 'Промоушен записи блокируется до прохождения аудита. История дефолтов калибрует PD-модель.'],
    ['Ничего не выдумываем', 'Отсутствующие данные показаны как отсутствующие. Никакой интерполяции, чтобы поле выглядело заполненным.'],
    ['Тесты на прайсинге и скоринге', 'Каждая функция прайсинга и скоринга покрыта тестом и прогоняется на каждом изменении.'],
  ] },
  { title: 'Данные и автоматизация', items: [
    ['Источники, одна схема', 'MOEX ISS, ЦБ, AZIPI, ГИР БО, Smartlab, новостные ленты, рейтинговые ленты приводятся к общей модели на входе.'],
    ['Конвейер три раза в сутки', 'Работает без оператора; расхождения уходят в коды здоровья, а не в тишину.'],
    ['37 команд CLI', 'Прямое управление для оператора: перезапуск участка, ручная правка рейтинга, форс-прогон скоринга.'],
    ['Библиотека аналитических навыков', 'event-classify, daily-brief, credit-scorecard, thesis-check, rate-analysis — повторяемые процедуры вместо разовых запросов.'],
  ] },
];

PAGES.overview = {
  render(el) {
    let n = 0;
    const groups = OVERVIEW_CAPABILITIES.map(g => `
      <div class="group">
        <div class="group-head">
          <div class="group-title">${escapeHtml(g.title)}</div>
          <div class="group-rule"></div>
          <div class="group-count">${g.items.length} ${plural(g.items.length, ['блок', 'блока', 'блоков'])}</div>
        </div>
        <div class="cap-grid">
          ${g.items.map(([title, body]) => {
            n += 1;
            return `<div class="cap">
              <div class="cap-head">
                <div class="cap-num${n > 8 ? ' is-alt' : ''}">${n}</div>
                <div class="cap-title">${escapeHtml(title)}</div>
              </div>
              <div class="cap-body">${escapeHtml(body)}</div>
            </div>`;
          }).join('')}
        </div>
      </div>`).join('');

    el.innerHTML = `<div class="page-overview">
      <div class="hero">
        <div class="hero-main">
          <div class="hero-kicker">Купон Лаб · вторая версия</div>
          <h1 class="hero-title">Анализ, который кредитный аналитик делает руками — выполняется автоматически.</h1>
          <p class="hero-lede">Каждое число прослеживается до источника. Ничего не выдумано: если данных нет, поле пустое, а не заполненное догадкой. Восемнадцать блоков — от скоринга РСБУ по шести опорам до мониторинга собственного конвейера данных.</p>
          <div class="hero-actions">
            <button type="button" class="pill-primary" data-page="terminal">Открыть карту и скринер</button>
          </div>
        </div>
        <div class="hero-aside">
          <div class="hero-aside-kicker">Что изменилось против v1</div>
          <div class="hero-aside-body">v1 показывала рынок. v2 его оценивает: доходность после ожидаемых потерь, выживаемость эмитента по годам, справедливый спред и разрыв с рыночным. Карта и скринер теперь одна поверхность — выделяете область на карте, таблица фильтруется на лету, из выборки собирается портфель.</div>
        </div>
      </div>

      <div id="overviewMarketBand" hidden></div>

      <div class="groups">${groups}</div>

      <div class="value-band">
        <div class="value-band-main">
          <div class="value-band-kicker">Ценность в одной строке</div>
          <div class="value-band-text">Это анализ, который кредитный аналитик сделал бы вручную, запущенный автоматически: каждое число прослеживается, ничего не придумано.</div>
        </div>
        <button type="button" class="value-band-btn" data-page="terminal">Смотреть в работе</button>
      </div>
    </div>`;

    if (typeof renderOverviewMarketBand === 'function') {
      renderOverviewMarketBand(document.getElementById('overviewMarketBand'));
    }
  },
};
