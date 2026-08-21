# GB-proxy

GB-proxy is an extensible HTTP proxy with first-class integrations for both
GEOBENCH and SymZilla on SymbOS. It also retains inherited support for other
constrained legacy web clients. It connects compatible early computers to the
modern Internet by simplifying HTML, rewriting long links, transliterating
text, rendering remote Markdown, and converting images into formats they can
display.

It is a downstream fork of
[MacProxy Plus](https://github.com/hunterirving/macproxy_plus), itself based on
[MacProxy](https://github.com/rdmark/macproxy). This fork adds dedicated support
for two client families:

- GEOBENCH's `BROWSER.APP`, using simplified HTML and portable GBPC v2 images;
- SymZilla on SymbOS, using bounded DOX documents and negotiated SGX images.

GB-proxy is the server-side transcoder, not the client network stack or browser
renderer. GEOBENCH renders the simplified HTML/GBPC response; SymZilla renders
the DOX/SGX response. Both clients connect to GB-proxy over plain HTTP, while
GB-proxy performs HTTP or HTTPS requests to upstream sites. Configure its
address as the client's proxy endpoint; opening the proxy root directly is not
a browsing interface.

## Quick start from a checkout

GB-proxy requires Python 3.9 or newer.

```shell
cp config.py.example config.py
```

Choose the setup for the client you are using. For GEOBENCH, enable its preset
in `config.py`:

```python
PRESET = "geobench"
```

SymZilla on SymbOS does not require a separate preset. It negotiates DOX and
SGX output on each request, so the same proxy process can serve both client
families without a separate SymZilla service. Use a
[network-enabled SymZilla](https://github.com/salvogendut/symapp-symzilla) on
SymbOS 3 or newer with a compatible running Network Daemon. The tested MSX
daemon is [symsys-networkdaemon-unapi](https://github.com/salvogendut/symsys-networkdaemon-unapi);
on CPC, SymZilla can use `NETD-M4C`.

Start the proxy:

```shell
./start_macproxy.sh --host 0.0.0.0 --advertise-host 192.168.1.10 --port 5001
```

The source launcher creates `venv/` and installs the project the first time it
is used. It does not install or upgrade packages on every proxy restart.

`--advertise-host` is the LAN address embedded in rewritten links. It is
especially important on multihomed hosts. Configure the resulting proxy address
on each client:

- in GEOBENCH's `BROWSER.APP`, open **Settings → Proxy** and enter the full
  URL, for example `http://192.168.1.10:5001`;
- in SymZilla, choose **Edit → Options...**, then enter `192.168.1.10:5001` or
  the full URL in **Network / GB proxy**.

The command listens on `127.0.0.1` by default. Binding to `0.0.0.0` deliberately
exposes it to the local network.

Useful commands:

```shell
venv/bin/gb-proxy --help
venv/bin/gb-proxy --config config.py --check-config
venv/bin/python -m unittest discover -s tests -v
```

On Windows, `start_macproxy.ps1` remains available for source-tree use.

## GEOBENCH (`BROWSER.APP`) profile

The `geobench` preset:

- reduces pages to the HTML subset supported by `BROWSER.APP`;
- retains links and compact GET forms;
- rewrites links and images to short proxy-local tokens;
- downloads and converts images lazily;
- bounds images to 160x96 pixels;
- emits GBPC v2 `.PIC` data, defaulting to canonical four-colour Mode 1;
- transliterates displayed text to printable 7-bit ASCII;
- minimizes response headers for constrained parsers.

Short tokens are held in a bounded, expiring in-memory registry. They therefore
expire after a configured idle period and do not survive a service restart.

When `.PIC` conversion is enabled, a client currently using MSX Screen 7 can
advertise `X-GBPC: 7,1` on each request. The proxy then returns 16-colour GBPC
Mode-7 images with the GEOBENCH MSX palette and two pixels per byte. A Mode-6
client can send `X-GBPC: 1` or omit the header; absent, malformed, and unknown
offers safely retain the byte-compatible Mode-1 output. `X-GBPC` is consumed by
the proxy and is not forwarded to upstream websites.

## Remote Markdown

GB-proxy renders remote Markdown for both supported client families. GEOBENCH
receives simplified HTML, while SymZilla receives the same bounded DOX
representation it negotiates for ordinary web pages. An upstream response is
recognized as Markdown when it uses the `text/markdown` media type or a legacy
`text/x-markdown`, `application/markdown`, or `application/x-markdown` alias.
For compatibility with simple file servers, a URL path ending in `.md` or
`.markdown` is also recognized when the server labels its response `text/plain`.

The supported safe subset includes headings, paragraphs, ordered and unordered
lists, emphasis, code spans and blocks, tables, links, and remote images.
GEOBENCH retains simple table markup; SymZilla renders the cell content in
reading order rather than as a grid.
Relative link and image destinations are resolved against the final remote
document URL after redirects. Remote HTTP or HTTPS images then pass through the
same download, size, conversion, and colour limits as images in HTML pages.

Raw HTML embedded in Markdown is inactive. It cannot create scripts, forms,
frames, links, image fetches, or other active browser elements. Markdown itself
has no supported form syntax; only forms from ordinary HTML pages can produce
the bounded controls described below. Excessively complex inline-code delimiter
layouts are rejected before parsing so a hostile document cannot monopolize a
proxy worker.

This support applies only to documents fetched through GB-proxy. SymZilla does
not parse local `.MD` or `.MARKDOWN` files, and opening one directly from its
file selector is not supported. Convert a local document to DOX separately or
serve it over HTTP with one of the recognized content types.

## SymbOS (SymZilla) DOX and SGX output

SymZilla support is selected per request and does not require a separate
GB-proxy preset. The browser requests a DOX representation and advertises the
active SymbOS screen capability:

```http
Accept: application/x-symbos-dox
X-GB-SGX: 0,4
```

The strict `X-GB-SGX` values are:

- `0,2` for two colours in SGX mode-0 packing;
- `0,4` for four colours in SGX mode-0 packing;
- `5,16` for sixteen colours in SGX mode-5 MSX packing.

Compatible SymZilla builds derive this value from the active screen rather than
just the host platform. GB-proxy honors the advertised profile; a missing,
malformed, or unsupported value safely defaults to `0,2`. This keeps generated
graphics within the capabilities reported by the client.

GB-proxy converts the upstream page into a bounded DOX document containing
`INFO`, `HEAD`, `TEXT`, `GRPH`, `LINK`, and `ENDF` chunks, plus an optional
`CTRL` chunk when usable forms are present. Page images are
downloaded eagerly, resized to at most 160x96, quantized against the fixed
SymbOS palette, and embedded as extended SGX graphic records. A directly
requested image is returned as a one-image DOX document. Scripts, active
content, and unsupported binary response types are not included.

Bounded GET forms support one-line text/search fields and submit buttons.
Hidden values and checked radio/checkbox defaults are retained in the short
action URL. POST forms, named submit values, and GET forms containing enabled
named password, file, text-area, select, or other unsupported controls are
omitted atomically; GB-proxy never emits a misleading partial form.
Activate the displayed submit button to send a form; pressing Enter is not
currently a submission shortcut.

This is a deliberately constrained HTML-to-DOX conversion, not a complete web
browser engine. It preserves useful text, headings, emphasis, links, supported
images, and the bounded GET controls above. Scripts and styles are removed,
complex layouts such as tables are flattened, and downloads, persistent login
sessions, and arbitrary browser controls are outside the supported subset.

SymZilla represents a proxy-generated link with a small eye icon after its
plain-text label. Activate the icon to follow the link; the label itself is not
the clickable control.

Links use short proxy-local URLs because SymZilla history entries hold 127
characters. Consequently, `--advertise-host` must be an address reachable from
the SymbOS machine, and links expire with the same bounded in-memory registry
used by the GEOBENCH preset. The proprietary request headers are consumed by
GB-proxy; upstream sites receive a conventional web/image `Accept` value.
Responses include `Vary: Accept, X-GB-SGX` so caches keep capability variants
separate.

## Configuration

The command searches for configuration in this order:

1. `--config PATH`;
2. `GB_PROXY_CONFIG`;
3. `./config.py`;
4. `/etc/gb-proxy/config.py`.

Presets and extensions remain compatible with the existing Python configuration
format. Long-running service limits—including timeouts, request sizes, cache
quota, and token lifetime—are documented in `config.py.example`.

Optional extension dependencies can be installed as Python extras:

```shell
venv/bin/python -m pip install --editable '.[anthropic]'
```

Available extras are `openai`, `anthropic`, `gemini`, and `mistral`.
SVG rendering uses the distribution-provided `rsvg-convert` utility. RPM
installations include it automatically. For source installations, install
`librsvg2-tools` on Fedora/EL or `librsvg2-bin` on Debian/Ubuntu. Without it,
HTML/text and raster-image conversion to GBPC or SGX continue to work, but SVG
images cannot be rendered.

## systemd service

The RPM installs `gb-proxy.service` but does not enable it automatically. One
service instance can serve both GEOBENCH and SymbOS/SymZilla; no separate
SymZilla service is required.

1. Edit `/etc/gb-proxy/config.py`. Enable the `geobench` preset for GEOBENCH;
   SymZilla needs no preset. Select any desired extensions for either client.
2. Edit `/etc/sysconfig/gb-proxy`.
3. For LAN access, set `GB_PROXY_HOST=0.0.0.0` and set
   `GB_PROXY_ADVERTISE_HOST` to the server's LAN address.
4. Open TCP port 5001 only on a trusted interface or zone.
5. Enable the service.

```shell
sudo systemctl enable --now gb-proxy.service
sudo systemctl status gb-proxy.service
journalctl -u gb-proxy.service
```

The unit runs as the unprivileged `gb-proxy` account. systemd owns the writable
paths:

- image cache: `/var/cache/gb-proxy`;
- extension state: `/var/lib/gb-proxy`;
- configuration: `/etc/gb-proxy/config.py`;
- service environment: `/etc/sysconfig/gb-proxy`.

The server uses one process and defaults to one request thread. Do not add
multiple worker processes: short tokens and some extension sessions are
intentionally process-local. Only increase `GB_PROXY_THREADS` when every enabled
extension is known to be thread-safe and client isolation is not required.

## RPM build

The application is pure Python and produces a `noarch` RPM. The spec is intended
for native Fedora and EL9-compatible builds with EPEL/CRB enabled; CI validates
Fedora 44. Automated GitHub releases are built on Fedora 44. `noarch` means
CPU-independent, not independent of distribution package dependencies.

Install the normal RPM build tools and the Python dependencies, then build a
committed checkout with:

```shell
./packaging/build-rpm.sh
```

Set `RPM_TOPDIR` to use a build tree other than `~/rpmbuild`, or pass a Git ref
as the first argument. The helper creates the source archive with `git archive`,
so uncommitted changes are not included.

For a tagged release, standard RPM tooling can fetch the sources declared by
the spec:

```shell
spectool -g -R gb-proxy.spec
rpmbuild -ba gb-proxy.spec
```

GitHub Actions also builds an RPM and source RPM for pushes to `master` and
pull requests targeting `master`. Pushing a version tag creates or updates the
corresponding GitHub Release and attaches both RPMs plus `SHA256SUMS`.

After updating all version locations to the next release, tag that version. For
example:

```shell
git tag -a v0.3.0 -m "GB-proxy 0.3.0"
git push origin v0.3.0
```

The tag must have the form `vN.N.N` and match the versions in
`gb-proxy.spec`, `setup.cfg`, `gb_proxy/__init__.py`, and the manual page.
Release RPMs are currently unsigned; their SHA-256 digests are published for
integrity checking.

RPM builds are offline after the declared sources and distribution packages
have been obtained. Dependencies are never downloaded by the service.

## Extensions

Extensions live under `extensions/` and are enabled through
`ENABLED_EXTENSIONS` in the configuration. Each extension declares a domain and
a request handler; some also provide a temporary global override mode.

Bundled extensions include:

- ChatGPT, Claude, Gemini, and Mistral text interfaces;
- Wikipedia;
- Reddit;
- Wayback Machine;
- Weather;
- Web Simulator;
- Hackaday and Hacksburg;
- NPR and Wiby;
- Kagi;
- `(not) YouTube`, which additionally requires `flimmaker`.

The AI extensions use API keys from the configuration file. The packaged file
is readable only by root and the `gb-proxy` service group.

## Security model

The client-to-proxy connection is intentionally plain HTTP for compatibility
with vintage systems. Treat the proxy as a trusted-LAN service:

- do not send passwords or other sensitive data through it;
- do not expose it directly to the Internet;
- restrict port 5001 with firewalld or an equivalent firewall;
- remember that enabled AI extensions can spend the configured API account;
- treat AI and override extensions as single-user: their process-global session
  state is shared by every client of the service;
- use the default loopback bind until LAN access is explicitly required.

The core proxy applies connect/read timeouts and response-size limits, uses a
fresh upstream session per client request, bounds memory and disk caches, and
does not write into its installed source tree.

## Historical demonstration

This inherited MacProxy demonstration illustrates the project's ancestry; it
is not a demonstration of the current GEOBENCH or SymZilla integrations.

<a href="https://youtu.be/f1v1gWLHcOk" target="_blank">
  <img src="./readme_images/youtube_thumbnail.jpg" alt="Teaching an Old Mac New Tricks" width="400">
</a>

Happy surfing.
