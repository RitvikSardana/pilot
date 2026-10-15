# Development sites behind a proxy

## Select the site

A proxy that sends requests directly to the development web port bypasses Pilot's production nginx. Frappe reads `X-Frappe-Site-Name` before `Host`. A client that sends the public hostname in that header can get a site-not-found error even when the proxy rewrites `Host` to the local site name.

For a site named `dev.localhost` exposed as `dev.example.com`, run this from the bench directory:

```bash
test -d sites/dev.localhost &&
  test ! -e sites/dev.example.com && test ! -L sites/dev.example.com &&
  ln -s dev.localhost sites/dev.example.com
```

The alias lets Frappe load the same site for either hostname. Pilot does not list the alias as another site, so use `dev.localhost` in Pilot commands. Keep the proxy's `Host` rewrite, such as Cloudflare Tunnel's `httpHostHeader: dev.localhost`. The checks require the local site directory and prevent replacement of an existing path or broken symlink.

Before you drop or rename the site, remove only the alias that points to it:

```bash
test -L sites/dev.example.com &&
  test "$(readlink sites/dev.example.com)" = dev.localhost &&
  unlink sites/dev.example.com
```

After a rename, create the alias again with the new site name. Pilot's production nginx sets `X-Frappe-Site-Name` to the configured site, so its domains do not need this alias.

## Keep redirects on the public URL

In developer mode, Frappe can append the bench's `webserver_port` to a site's `host_name` when that URL has no port. A proxy serving HTTPS on port 443 can then redirect the browser to `https://dev.example.com:8000`, which the proxy does not serve.

Before you change `host_name`, record its current value or that it is unset. This command prints only that setting:

```bash
python3 -c 'import json; from pathlib import Path; config = json.loads(Path("sites/dev.localhost/site_config.json").read_text()); print("host_name =", repr(config["host_name"]) if "host_name" in config else "(unset)")'
```

Set the site's public URL with an explicit port:

```bash
pilot -b dev frappe --site dev.localhost set-config host_name https://dev.example.com:443
```

Use the port that the browser reaches: `443` for standard HTTPS, `80` for standard HTTP, or the proxy's custom port. Keep the bench's internal `webserver_port` unchanged. When you remove the proxy, set `host_name` back to the recorded value. If it was unset, run `pilot -b dev frappe --site dev.localhost set-config host_name None` to remove the key.

## Verify the public URL

The site alias selects the site. `host_name` controls generated URLs. Configure Socket.IO routing separately. Through the public hostname, check:

- A page load.
- A login redirect stays on the public URL.
- An authenticated API call with `X-Frappe-Site-Name: dev.example.com` reaches the site.
- A realtime event reaches the intended site through Socket.IO.
