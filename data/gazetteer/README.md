# Country gazetteers

One file per country. These drive Tiers 2–3 of the classifier (ARCHITECTURE_V2 §8)
and are the single largest lever on classification accuracy — §A10 calls
wrong-country-on-the-homepage the worst failure this product has.

## Fields

| field | meaning |
|---|---|
| `alias` | the surface string to match, lowercased, matched on word boundaries |
| `type` | `name` \| `demonym` \| `adjective` \| `capital` \| `city` \| `region` \| `leader` \| `iso` \| `currency` \| `institution` |
| `weight` | contribution to the country's score when matched |
| `ambiguous` | true when the string also means something else |
| `context` | required disambiguating terms — the alias scores **only** if one appears |

## Weights

    1.0   unambiguous country name ("Bangladesh")
    0.9   capital, unambiguous major city, national institution
    0.8   demonym / adjective ("Brazilian")
    0.6   well-known secondary city
    0.4   currency, ISO code, leader surname
    0.2   ambiguous alias that cleared its context gate

Nothing scores above 1.0. A single mention of a capital should not outweigh
an explicit country name.

## The ambiguity rule

An alias marked `ambiguous: true` contributes **nothing** unless one of its
`context` terms also appears in the text. This is deliberately strict: the
cost of missing a country is one article filed under "International", while
the cost of a false positive is a reader seeing a US crime story on the
Georgia country page. Those are not symmetric.

Known traps encoded here:

* **Georgia** — country vs US state
* **Jordan** — country vs given name (Michael Jordan, Jordan Henderson)
* **Turkey** — country vs the bird
* **Victoria** — Australian state vs Canadian city vs Seychelles capital
* **Birmingham** — England vs Alabama
* **Perth** — Australia vs Scotland
* **London** — England vs Ontario
* **Sydney** — Australia vs Nova Scotia
* **Cambridge / Oxford** — England vs Massachusetts
* **Hyderabad** — India vs Pakistan
* **Newcastle** — England vs Australia
* **Wellington** — New Zealand vs UK usages
* **Cork / Reading / Nice / Bath** — city names that are common words

`data/gazetteer/_ambiguous.yaml` holds cross-country conflicts that cannot be
expressed inside a single country file.
