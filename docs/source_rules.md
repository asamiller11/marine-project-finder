# Source rule

A source whose robots.txt disallows automated reading may be read only if (a) the site owner has given written permission, or (b) its official terms or open-data licence explicitly invite automated access (for example a documented API or bulk download page). Never work around a 401, 403 or 429 or any block.

## What this means in practice

- Before the first request to a website, MitiMark reads its robots.txt and obeys it.
- If robots.txt disallows a page, MitiMark does not read it. The only exceptions are the two in the rule:
  - **(a)** The site owner has given written permission. Keep the email or letter in `private/`.
  - **(b)** The site's official terms or open-data licence explicitly invite automated access, for example
    a documented API or a bulk download page.
- An exception is written into the source's block in `config/settings.yaml`. Without a complete block,
  robots.txt applies in full:

```yaml
sources:
  example_source:
    enabled: false
    robots_exception:
      basis: b                      # a = written permission, b = terms or licence invite automated access
      hosts: ["data.example.org"]   # only these hosts; other hosts of the source still follow robots.txt
      evidence: "https://data.example.org/api-terms (documented API, open licence), checked 2026-11-17"
```

- An exception covers only the source it is written under, and only the hosts it lists.
- Every run prints a line `Robots.txt exceptions used this run: ...` naming each source that read a page
  robots.txt alone would have refused, how many pages, the basis and the evidence.
- A 401, 403 or 429 always stops the source, with or without an exception. MitiMark asks that website
  nothing more for the rest of the run and never retries with other headers, browsers, proxies or tools.
- The `never_fetch` list in `config/settings.yaml` always wins, even over an exception.
- A source must also have a licence that allows use in a paid alert service with credit, before it is
  switched on. Sources with unclear terms stay off until permission is given.
