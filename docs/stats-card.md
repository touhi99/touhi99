# Stats card

`stats/generate.py` renders `stats/card.svg` from the GitHub GraphQL API. The `stats card` workflow runs it daily and commits the card when it changes.

## Data rules

- The window is the last 365 days. The contribution calendar is Sunday-aligned and can return a few days outside that window; those days are dropped before any stat is computed.
- Repositories are counted from the contribution lists without storing their names, so the card prints no repository name.
- The current streak skips today when today has no contributions yet, so a streak does not reset in the morning.
- When the token has no access to private contributions, the private tile is dropped rather than shown as zero.

## Excluded repositories

`STATS_EXCLUDE` is a comma-separated list of repository names left out of the repository and language stats: rendered artefacts, vendored bundles and other repositories whose size does not reflect the languages worked in. Contribution counts still include them. The workflow reads it from the repository secret of the same name, which keeps the names out of the public run logs.

## Look

- Both palettes are embedded in the SVG and chosen with `prefers-color-scheme`.
- The light `muted` colour `#6F675C` has a 5.25:1 contrast ratio on the light background, enough for 8 to 10 px labels.
- The delta next to a secondary stat is placed using a rough proportional-digit width of the value.

## Checks

`scripts/check` runs ruff (format and lint), mypy and the docs and comment lint. It needs `uv`.
