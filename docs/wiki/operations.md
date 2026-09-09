# Эксплуатация и диагностика

## Кратко

- Автоматический запуск на сервере: **каждый час** через `price-feed.timer`.
- Первый запуск после старта сервера: через пять минут.
- Ручной запуск на сервере: `sudo systemctl start price-feed.service`.
- Результат: `/var/www/price-feed/feed.xml`.
- При неуспешном запуске последняя корректная версия файла сохраняется.

Проверить, что ежечасный таймер включён и увидеть следующий запуск:

```sh
systemctl status price-feed.timer
systemctl list-timers price-feed.timer
```

## Ручной запуск

На установленном сервере:

```sh
sudo systemctl start price-feed.service
sudo journalctl -u price-feed.service -n 50 --no-pager
```

Команда `systemctl start` ожидает завершения генерации. После неё в журнале должно быть `"event": "success"`. Если сервис завершился с ошибкой, посмотреть полный журнал:

```sh
sudo journalctl -u price-feed.service --since "1 hour ago" --no-pager
```

Локальный ручной запуск с реальным Shopify и локальными защищёнными файлами:

```sh
cd /ПУТЬ/К/price-feed
set -a
source .env
set +a
python3 feed.py --config config.local.json
```

Локальные `.env`, `config.local.json` и каталог `runtime/` исключены из Git.

Локальная демонстрация без обращения к Shopify:

```sh
python3 -m unittest discover -s tests -v
python3 feed.py --config config.example.json --fixture examples/catalog.jsonl
```

## Успешный журнал

Генератор печатает одну JSON-строку с полями:

- `event=success`;
- время UTC и длительность;
- количество товаров, вариантов и offers;
- количество фильтраций и пропусков;
- причины пропусков и до 100 безопасных variant ID.

Секреты, подписанные URL и полные ответы API в журнал не выводятся.

## Частые причины пропусков

| Код | Значение |
|---|---|
| `unknown_stock` | Остаток не отслеживается или неизвестен. |
| `invalid_price` | Цена пустая, нулевая, отрицательная или некорректная. |
| `invalid_https_url` | Нет корректной HTTPS-ссылки товара или изображения. |
| `missing_categoryId` | Пустой или ещё не сопоставленный productType. |
| `market_currency_mismatch` | Валюта рыночной цены не совпадает с конфигом. |

## Ошибки всего запуска

- `oauth_http_401`, `http_401`, `403`: проверить установку приложения, секрет или токен и scopes.
- `graphql_error`, `bulk_submission_rejected`: проверить версию API, запрос и доступ.
- `bulk_timeout_resume_next_run`: следующий запуск продолжит сохранённую операцию.
- `bulk_count_mismatch`: JSONL скачан не полностью; старый XML сохранён.
- `duplicate_variant_id`: обнаружен дубликат стабильного ID; обновление отклонено.
- `already_running`: другой процесс уже выполняет обмен.
- `ParseError`, `OSError`, `PermissionError`: проверить данные, диск и права.

## Проверка опубликованного файла

```sh
curl -I --resolve ВАШ-ДОМЕН:443:127.0.0.1 \
  https://ВАШ-ДОМЕН/feeds/price-feed.xml
xmllint --noout /var/www/price-feed/feed.xml
```

Проверка с произвольного внешнего IP должна вернуть `403`. Запрос со стороны КИП должен вернуть `200` и XML. После этого выполняется тестовый импорт КИП.

## Регулярный контроль

- Проверять возраст `feed.xml` и время следующего запуска таймера.
- Следить за ростом `skipped` и изменениями причин.
- Сверять количество offers с обычным диапазоном каталога.
- Периодически проверять актуальность Shopify API version и IP КИП.
- После изменений запускать все тесты и один полный Bulk-экспорт.
