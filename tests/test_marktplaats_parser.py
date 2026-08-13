from __future__ import annotations

from marktplaats_ad_watcher.marktplaats import normalize_ads


def test_normalize_lrp_listing_payload() -> None:
    payload = {
        "listings": [
            {
                "itemId": "m1234567890",
                "title": "Complete racing bike",
                "description": "Well maintained bike with receipts.",
                "vipUrl": "/v/fietsen-en-brommers/fietsen-racefietsen/m1234567890",
                "priceInfo": {"priceCents": 12500},
                "location": {"cityName": "Eindhoven", "distanceMeters": 12000},
                "sellerInformation": {"sellerName": "Sam"},
                "pictures": [
                    {"mediumUrl": "https://images.marktplaats.com/example-medium.jpg"},
                    {"largeUrl": "https://images.marktplaats.com/example-large.jpg"},
                ],
            }
        ]
    }

    ads = normalize_ads(payload)

    assert len(ads) == 1
    assert ads[0].id == "m1234567890"
    assert ads[0].title == "Complete racing bike"
    assert ads[0].url == "https://www.marktplaats.nl/v/fietsen-en-brommers/fietsen-racefietsen/m1234567890"
    assert ads[0].price == "EUR 125.00"
    assert ads[0].location == "Eindhoven, 12.0 km"
    assert ads[0].seller == "Sam"
    assert ads[0].image_urls == [
        "https://images.marktplaats.com/example-medium.jpg",
        "https://images.marktplaats.com/example-large.jpg",
    ]


def test_normalize_ignores_duplicate_ids() -> None:
    payload = {
        "listings": [
            {"itemId": "m1", "title": "First", "vipUrl": "/v/m1", "description": "A"},
            {"itemId": "m1", "title": "First duplicate", "vipUrl": "/v/m1", "description": "B"},
        ]
    }

    ads = normalize_ads(payload)

    assert len(ads) == 1
    assert ads[0].title == "First"
