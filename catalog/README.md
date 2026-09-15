# catalog/

`ingredients.json` is a **verbatim copy** of the Flutter app's ingredient catalog
(`assets/data/ingredients.json` in the app repository, `C:\AI\Jídlo` locally).
The app repository is the source of truth - never edit the copy here.

The mapper (`pipeline/mapper.py`) reads it for:

* `id`, `nameCs`, `namePluralCs`, `aliases` - the names an offer title is matched against
  (folded and stemmed exactly like `lib/logic/text_normalizer.dart` does in the app);
* `category` - category sanity checks (a "smetana" offer must land on a dairy id);
* `unitGrams.ks` - price per piece -> per kg (eggs: 55 g);
* `densityGPerMl` - price per litre -> per kg;
* `refPriceCzkPerKg` - a computed price outside 0.2-4x of the reference is sent to review.

`category_fallback.json` (if present) is a snapshot of the app's `categoryFallbackCzkPerKg`
used by `pipeline/build_prices.py`.

## Re-sync after the app catalog changes

```sh
python -m pipeline.mapper --sync-catalog                                   # default: C:\AI\Jídlo\assets\data\ingredients.json
python -m pipeline.mapper --sync-catalog /path/to/app/assets/data/ingredients.json
python run_all.py --sync-catalog "C:/AI/Jídlo"                            # ingredients + category fallbacks
python -m pytest tests/test_mapper.py                                     # the fixtures must still map
```

Commit the refreshed copy together with any rule changes in `data/mappings.json` that the
new ids require. Ingredient ids referenced by `docs/prices.json` are validated against this
file by `pipeline/validate.py`, so a removed id shows up as a validation error.
