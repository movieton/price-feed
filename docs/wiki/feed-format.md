# Формат фида и отображение данных

Целевая структура основана на предоставленном образце `examples/feed-template.xml`. Она не заменяется форматами Google или Meta.

## Структура

```xml
<yml_catalog date="2026-09-09 11:23">
  <shop>
    <name>Routes&amp;Roads</name>
    <company>Routes&amp;Roads</company>
    <url>https://www.routesandroads.fr</url>
    <categories>...</categories>
    <offers>
      <offer id="123" available="true">
        <name>Название — вариант</name>
        <url>https://www.routesandroads.fr/products/example?variant=123</url>
        <price>99.90</price>
        <currencyId>EUR</currencyId>
        <categoryId>456</categoryId>
        <picture>https://cdn.shopify.com/...</picture>
      </offer>
    </offers>
  </shop>
</yml_catalog>
```

## Источники полей

| XML | Источник Shopify и правило |
|---|---|
| `offer@id` | Числовая часть стабильного Shopify ProductVariant GID. |
| `offer@uuid` | Только подтверждённая внешняя связка из `extra_data`; автоматически не создаётся. |
| `offer@available` | `true`, только если остаток отслеживается и суммарный `inventoryQuantity > 0`; иначе `false` или пропуск при неизвестном остатке. |
| `name` | Название товара плюс значения опций варианта; `Default Title` не добавляется. |
| `url` | `onlineStoreUrl` с query-параметром `variant`. |
| `price` | Положительная цена Decimal без символа валюты. |
| `currencyId` | Проверенный трёхбуквенный код из `currency`; сейчас `EUR`. |
| `categoryId` | Явное соответствие точного Shopify `productType` стабильному ID категории. |
| `picture` | Картинка варианта, иначе главная картинка товара. |
| `vendor` | Производитель Shopify, если заполнен. |
| `barcode` | Штрихкод варианта, если заполнен; ведущие нули сохраняются. |
| `param name="Артикул"` | SKU варианта, если заполнен. |

Разрешённые названия параметров из образца: `Артикул`, `Цвет`, `Пол`, `Возраст`, `Сезон`, `Вид активности`, `Тип товара`, `Модельный год`, `Назначение`. Регистр сохраняется. Неподтверждённые значения не угадываются.

## Обязательные поля и пропуски

Рабочая проверка требует `offer@id`, `name`, `url`, положительную `price`, `currencyId`, `categoryId` и `picture`. Если один вариант не проходит проверку, исключается только он. Если отсутствует общее поле товара, будут исключены связанные варианты этого товара.

Дубликат идентификатора считается ошибкой всего файла, потому что его нельзя безопасно разрешить пропуском одной строки.

## Валюта

`currencyId` добавляется при каждой генерации сразу после `price`. В режиме базовой цены валюта конфига сверяется с `shop.currencyCode`. В режиме market валюта каждой цены сверяется с `contextualPricing.price.currencyCode`.
