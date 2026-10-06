# Vendored Markdown parser

DevHelper bundles **markdown-it 15.0.2** (MIT) for offline Markdown previews. This is the unmodified UMD browser artifact `package/dist/browser/markdown-it.umd.min.js` from the published npm package, renamed locally to `markdown-it.min.js`. It exposes the global callable `window.markdownit` and the usual `.parse()` and `.render()` APIs. Browser-standard `atob` is required by the bundled entity decoder.

- Official repository and version: https://github.com/markdown-it/markdown-it/tree/15.0.2
- Official package definition: https://github.com/markdown-it/markdown-it/blob/15.0.2/package.json
- Official documentation: https://markdown-it.github.io/markdown-it/
- Canonical package archive: https://registry.npmjs.org/markdown-it/-/markdown-it-15.0.2.tgz
- Retrieval archive: https://registry.npmmirror.com/markdown-it/-/markdown-it-15.0.2.tgz
- Retrieval metadata: https://registry.npmmirror.com/markdown-it/15.0.2
- Browser asset size: 115,080 bytes
- Browser asset SHA-256: `635972b985228e8af9f0143647c68616b7a3bb09f6946e7e4a52e43dcf5e7be5`
- Archive SHA-1: `5bc092394e9693519f6d5f499d3575469fec62f3`
- Archive SRI: `sha512-q4IGxMv56jCqT4OCRCADBoDP3LO4MhmTXjFbphHPXs4g3j9Xg5RDnxqN8IF/3vIWEU+VCnUq+7JUg/cfy2E6Qw==`

Direct access to the canonical registry was unavailable during retrieval, so the npm package mirror supplied the archive. The archive SHA-1 and SHA-512 SRI were verified against that mirror's package metadata before extracting only the browser bundle and license. The official repository tag and documentation independently confirm the version and browser entry point. No source maps or runtime network dependencies are bundled. Exact machine-readable provenance is in `markdown-it.provenance.json`; upstream license text is in `markdown-it.LICENSE`.

The preview disables HTML, linkification, typographic substitutions and implicit hard breaks. Markdown-it's built-in link validation rejects `javascript:`, `vbscript:`, `file:` and unsafe data URLs, but allows ordinary HTTP URLs and certain raster data-image URLs. The app's DOM renderer applies its own stricter attachment and navigation rules and never copies arbitrary HTML or attributes into the preview.
