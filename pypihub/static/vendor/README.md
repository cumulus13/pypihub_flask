# Vendored front-end assets

Served from this server so the interface works without internet access. Do not edit; replace by upgrading.

| Asset | Version | License | Source |
|---|---|---|---|
| Bootstrap CSS and JS (bundle) | 5.3.8 | MIT (`bootstrap/LICENSE`) | npm `bootstrap` |
| Bricolage Grotesque Variable (latin, wght) | 5.3.0 of `@fontsource-variable/bricolage-grotesque` | SIL OFL 1.1 (`../fonts/LICENSE-BricolageGrotesque.txt`) | npm |
| JetBrains Mono Variable (latin, wght) | 5.3.0 of `@fontsource-variable/jetbrains-mono` | SIL OFL 1.1 (`../fonts/LICENSE-JetBrainsMono.txt`) | npm |
| Icons (inlined in `templates/_sprite.html`) | Bootstrap Icons 1.13.2 | MIT | npm `bootstrap-icons` |

The only change to the Bootstrap files is removing the trailing `sourceMappingURL` comment, so browsers do not
request `.map` files that are not shipped.
