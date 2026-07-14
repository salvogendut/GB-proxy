# GB-proxy

GB-proxy is an extensible HTTP proxy that connects GEOBENCH and other early
computers to the modern Internet. It simplifies HTML, rewrites long links,
transliterates text, and converts images into formats that constrained clients
can display.

It is a downstream fork of
[MacProxy Plus](https://github.com/hunterirving/macproxy_plus), itself based on
[MacProxy](https://github.com/rdmark/macproxy). This fork adds a GEOBENCH
compatibility profile and portable GBPC v2 image output.

## Quick start from a checkout

GB-proxy requires Python 3.9 or newer.

```shell
cp config.py.example config.py
```

For GEOBENCH, enable its preset in `config.py`:

```python
PRESET = "geobench"
```

Start the proxy:

```shell
./start_macproxy.sh --host 0.0.0.0 --advertise-host 192.168.1.10 --port 5001
```

The source launcher creates `venv/` and installs the project the first time it
is used. It does not install or upgrade packages on every proxy restart.

`--advertise-host` is the LAN address embedded in rewritten links. It is
especially important on multihomed hosts. In `BROWSER.APP`, enter the resulting
proxy URL, for example `http://192.168.1.10:5001`.

The command listens on `127.0.0.1` by default. Binding to `0.0.0.0` deliberately
exposes it to the local network.

Useful commands:

```shell
venv/bin/gb-proxy --help
venv/bin/gb-proxy --config config.py --check-config
venv/bin/python -m unittest discover -s tests -v
```

On Windows, `start_macproxy.ps1` remains available for source-tree use.

## GEOBENCH profile

The `geobench` preset:

- reduces pages to the HTML subset supported by `BROWSER.APP`;
- retains links and GET/POST forms;
- rewrites links and images to short proxy-local tokens;
- downloads and converts images lazily;
- bounds images to 160x96 pixels;
- emits canonical four-colour GBPC v2 Mode-1 `.PIC` data;
- transliterates displayed text to printable 7-bit ASCII;
- minimizes response headers for constrained parsers.

Short tokens are held in a bounded, expiring in-memory registry. They therefore
expire after a configured idle period and do not survive a service restart.

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
`librsvg2-tools` on Fedora/EL or `librsvg2-bin` on Debian/Ubuntu. SVG rendering
is optional; raster image conversion and GBPC output do not require it.

## systemd service

The RPM installs `gb-proxy.service` but does not enable it automatically.

1. Edit `/etc/gb-proxy/config.py` and select the desired preset/extensions.
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

The application is pure Python and produces a `noarch` RPM. The spec targets
Fedora and EL9-compatible systems with EPEL/CRB enabled.

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

## Demonstration

<a href="https://youtu.be/f1v1gWLHcOk" target="_blank">
  <img src="./readme_images/youtube_thumbnail.jpg" alt="Teaching an Old Mac New Tricks" width="400">
</a>

Happy surfing.
