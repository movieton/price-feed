# Установка и публикация на сервере

Генератор можно установить на отдельный Linux-сервер. Сервер сайта не требуется.

## Требования

- Linux с systemd либо cron;
- Python 3.11+ без дополнительных pip-пакетов;
- исходящий HTTPS к Shopify;
- Nginx и действительный TLS-сертификат;
- отдельный домен или поддомен, например `feed.routesandroads.fr`.

## Рекомендуемые пути

| Назначение | Путь |
|---|---|
| Код | `/opt/price-feed` |
| Секреты | `/etc/price-feed/credentials.env` |
| Конфигурация | `/etc/price-feed/config.json` |
| Состояние | `/var/lib/price-feed` |
| Опубликованный файл | `/var/www/price-feed/feed.xml` |

## Установка

1. Клонировать репозиторий в `/opt/price-feed`.
2. Создать системного пользователя `pricefeed`.
3. Создать каталоги конфигурации, состояния и публикации с правами из корневого README.
4. Сохранить секреты вне репозитория и заполнить live-конфиг.
5. Скопировать `deploy/price-feed.service` и `deploy/price-feed.timer` в `/etc/systemd/system/`.
6. Добавить содержимое `deploy/nginx-location.conf` внутрь нужного HTTPS `server` блока.
7. Проверить конфигурацию Nginx и первый запуск.

```sh
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl start price-feed.service
sudo journalctl -u price-feed.service -n 30 --no-pager
sudo systemctl enable --now price-feed.timer
systemctl list-timers price-feed.timer
```

## Расписание

Таймер запускает первый обмен через пять минут после старта сервера и затем раз в час:

```ini
OnBootSec=5min
OnUnitActiveSec=1h
```

## HTTPS и доступ КИП

Ожидаемый адрес:

```text
https://ВАШ-ДОМЕН/feeds/price-feed.xml
```

Nginx разрешает `GET` и `HEAD` с исходного IP `5.129.221.173`, который на момент настройки соответствует `app.marketing-calendar.ru`. Локальные `127.0.0.1` и `::1` разрешены для диагностики; остальные IP получают `403`.

Домен `app.marketing-calendar.ru` принадлежит клиенту, который скачивает фид. Сам файл размещается на домене сервера генератора. Если IP КИП изменится, allowlist потребуется обновить.
