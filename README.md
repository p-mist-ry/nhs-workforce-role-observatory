# UK NHS Workforce Role Change Observatory

An independent research observatory that collects public NHS and NHS-funded
job adverts across England, Scotland, Wales and Northern Ireland every day,
and analyses **how work is changing**, with a particular focus on the policy
question:

> **How is AI changing roles and the kinds of work?**

It is built to be auditable: every number on the dashboard can be traced back
to stored adverts, the exact text that was used as evidence, and the version
of the rules, prompts and code that produced it.

> Status at creation: **no source collects yet.** Every collector is
> deliberately switched off until you have checked the official feed
> documentation and terms of use. Until then the daily run records
> `BLOCKED_NOT_CONFIGURED` and fetches nothing.

---

## How the ten principles are built in

| # | Principle | Where it is enforced |
|---|-----------|----------------------|
| 1 | Never classify from job title alone | `classify_rules.py` scores only description text; title matches are kept as hints. `validate.py` fails any role label without description evidence. Prompts repeat the rule. |
| 2 | Preserve source URLs and evidence | Every observation keeps `request_url` and `record_url`; every classification keeps the matched text and character offsets. |
| 3 | Never invent missing values | Parsers return `null`; unresolvable nations are `UNKNOWN`; "Hours: Various" stays `null`. `validate.py` rejects empty strings. |
| 4 | Separate observed facts from inferences | Normalised records have an `observed` block and an `inferred` block. |
| 5 | Separate advert demand from workforce stock | `sources.yaml` labels each source `demand`, `stock` or `context`; the dashboard states that counts are adverts. |
| 6 | Preserve nation | Nation is inferred from provider register, single-nation source, or postcode, with method and confidence; conflicts become `UNKNOWN`. |
| 7 | No double counting | `deduplicate.py` merges repeat sightings and cross-posts into vacancy clusters; one cluster is one unit of demand. |
| 8 | Append-only logs | `validate.py` fails if any existing log line or stored observation is edited or deleted. |
| 9 | GenAI evidence and confidence | `classify_genai.py` rejects any output whose quotes are not found verbatim in the advert, whose labels are not in the taxonomy, or whose confidence is not 0–1. |
| 10 | Versioned changes | Taxonomy, lexicon, sources, settings, prompts and code each carry a version. `validate.py` fails if content changes without a version bump and a `change_log.csv` row. |

---

## Repository map

```
README.md                  this guide
requirements.txt           the single dependency (PyYAML)
.gitignore                 intermediate files that are rebuilt, not stored
config/
  sources.yaml             every data source; all endpoints null until verified
  providers.csv            employer register used to infer nation and sector
  taxonomy.yaml            role families, task categories, AI relationships
  ai_lexicon.yaml          AI terms, with false-positive guards
  pipeline.yaml            de-duplication, change detection and GenAI settings
data/
  observations/            raw adverts, append-only, one new file per run
  derived/                 processing outputs (most rebuilt each run)
    changes/               indicators, change signals, term emergence, re-advert diffs
    genai_runs/            GenAI outputs, append-only
  dashboard/summary.json   the record of what the dashboard showed
logs/
  runs.csv                 one row per script run, with versions and fingerprints
  change_log.csv           every change to taxonomy, lexicon, prompts, code, settings
  qa_log.csv               every validation check result
prompts/                   versioned GenAI prompts
scripts/                   the pipeline (Python 3, standard library + PyYAML)
tests/                     automatic tests, run before every daily collection
docs/                      the public dashboard (GitHub Pages)
.github/workflows/daily.yml   the daily automation
```

### What runs each day, in order

1. **Unit tests.** If they fail, nothing else runs.
2. **Collect** from each demand source (`collect_*.py`). A source that is not
   fully configured is skipped and the reason logged.
3. **Normalise** (`normalize.py`): pay, band, hours, dates, contract, postcode
   and nation are extracted with fixed rules, each with its evidence text.
4. **Deduplicate** (`deduplicate.py`): repeated and cross-posted adverts
   become one vacancy cluster.
5. **Classify with rules** (`classify_rules.py`): role family, clinical
   status, task categories and AI mentions, ignoring employer boilerplate.
6. **Classify with GenAI** (`classify_genai.py`): off unless you enable it.
7. **Detect changes** (`detect_changes.py`).
8. **Validate** (`validate.py`) and write `logs/qa_log.csv`.
9. **Build dashboard data** and **commit** observations, logs and outputs.

---

## Part 1 – Setting up the repository in your browser

You need a free GitHub account. Every step below happens on github.com.

### 1.1 Create the repository

1. Go to **github.com** and sign in.
2. Click the **+** icon at the top right of the page, then **New repository**.
3. In **Repository name** type `nhs-role-observatory`.
4. Choose **Public**. (Free GitHub Pages websites need a public repository.
   Everything stored will be visible to anyone; see "Privacy" below.)
5. Tick **Add a README file**.
6. Click the green **Create repository** button.

### 1.2 Replace the README with this guide

1. On the repository page, click **README.md** in the file list.
2. Click the **pencil icon** (Edit this file) at the top right of the file.
3. Click inside the editor, press **Ctrl+A** (Windows) or **Cmd+A** (Mac),
   then **Delete**.
4. Paste the contents of this README.
5. Click the green **Commit changes...** button at the top right.
6. In the box that appears, leave **Commit directly to the main branch**
   selected and click **Commit changes**.

### 1.3 Create every other file (repeat for each file)

1. Go to the repository's main page (click the repository name at the top).
2. Click the **Add file** button (above the file list, left of the green
   **Code** button) and choose **Create new file**.
3. In the **Name your file...** box, type the full path, for example
   `config/sources.yaml`. When you type `/`, GitHub turns the part before it
   into a folder automatically.
4. Click in the large editor area and paste the file's contents.
5. Click **Commit changes...**, keep **Commit directly to the main branch**,
   and click **Commit changes**.

Create the files **in this order**. The workflow file is last on purpose, so
no automatic run starts before everything is in place.

```
requirements.txt
.gitignore
config/sources.yaml
config/providers.csv
config/taxonomy.yaml
config/ai_lexicon.yaml
config/pipeline.yaml
logs/runs.csv
logs/change_log.csv
logs/qa_log.csv
prompts/extraction.md
prompts/classification.md
prompts/verification.md
prompts/change_detection.md
scripts/common.py
scripts/collect_core.py
scripts/collect_nhsjobs.py
scripts/collect_scotland.py
scripts/collect_hscni.py
scripts/normalize.py
scripts/deduplicate.py
scripts/classify_rules.py
scripts/classify_genai.py
scripts/detect_changes.py
scripts/validate.py
scripts/build_dashboard_data.py
tests/helpers.py
tests/test_normalize.py
tests/test_classify_rules.py
tests/test_deduplicate.py
tests/test_collect_failsafe.py
tests/test_genai_checks.py
data/observations/README.md
data/derived/README.md
data/dashboard/README.md
docs/.nojekyll
docs/index.html
docs/styles.css
docs/app.js
.github/workflows/daily.yml
```

**Faster alternative.** If you downloaded the zip of all files: unzip it on
your computer, then on the repository page click **Add file > Upload files**
and drag the *contents* of the unzipped folder into the box (Chrome and Edge
keep the folder structure). Files whose names start with a dot
(`.gitignore`, `docs/.nojekyll`, `.github/workflows/daily.yml`) are often
hidden by Mac Finder and Windows Explorer; check they arrived, and create any
missing ones with the steps in 1.3. Upload the workflow file last.

### 1.4 Allow the workflow to save its results

1. Click the **Settings** tab (top of the repository page; if you cannot see
   it, click the **...** menu at the end of the tab row).
2. In the left sidebar click **Actions**, then **General**.
3. Scroll to **Workflow permissions**.
4. Select **Read and write permissions**.
5. Click **Save**.

### 1.5 Turn on the dashboard website

1. In **Settings**, click **Pages** in the left sidebar.
2. Under **Build and deployment**, set **Source** to **Deploy from a branch**.
3. Under **Branch**, choose **main**, then choose the folder **/docs**.
4. Click **Save**.
5. After two to five minutes, refresh the page. A box at the top shows your
   site address, ending in `.github.io/nhs-role-observatory/`.

### 1.6 Run the pipeline for the first time

1. Click the **Actions** tab.
2. If GitHub shows a message asking to enable workflows, click the green
   button to enable them.
3. In the left list click **Daily observatory run**.
4. On the right, click **Run workflow**, then the green **Run workflow**
   button in the dropdown.
5. Wait about a minute and refresh. A green tick means success.

**What to expect:** the collectors report that they are not configured, and
nothing is collected. That is correct. Open `logs/runs.csv` (click
**logs**, then **runs.csv**) to see rows with status
`BLOCKED_NOT_CONFIGURED` and the reasons. The dashboard will show that no
source is switched on yet.

---

## Part 2 – Switching on a data source

No endpoint has been filled in for you. This is deliberate: an endpoint must
be copied from official documentation, not guessed.

### 2.1 NHS Jobs (England and Wales)

The NHS Business Services Authority (NHSBSA) publishes a Word document called
**"NHS Jobs Self-Serve Job Adverts API"**, revised several times (for
example version 1.07). It describes an XML search endpoint and an RSS feed.

1. Search the web for `NHS Jobs Self-Serve Job Adverts API nhsbsa` and
   download the **newest** version from the nhsbsa.nhs.uk website.
2. Read it, and the NHS Jobs terms of use. Note the exact endpoint address,
   the parameters it supports, how many results it returns per request, and
   how paging works.
3. Open the example XML in the document. Note the name of the element that
   wraps each single vacancy, and the element names for title, employer,
   location, salary, closing date, description and advert URL.
4. In GitHub, open `config/sources.yaml`, click the **pencil icon**, and in
   the `nhsjobs` block fill in, using exactly what the document says:
   * `enabled: true`
   * `documentation_url:` the address you downloaded the document from
   * `endpoint:` the exact endpoint address from the document
   * `format: xml` (or `rss` if you chose the RSS feed)
   * `record_path:` the element name that wraps one vacancy
   * `params:` any documented parameters you need
   * `pagination:` as documented (or leave `mode: none`)
   * `field_map:` for example `title: <element name>`; `url` and `title`
     are required
   * `terms_url:` and `terms_reviewed_on:` (today's date, `YYYY-MM-DD`)
5. Near the top of the same file, replace `CHANGE_ME@example.org` in
   `user_agent` with a real contact e-mail address.
6. Change the file's `version:` line from `0.1.0` to `0.2.0`.
7. Commit the change.
8. Open `logs/change_log.csv`, click the pencil, add one line **at the very
   bottom** (never edit earlier lines), and commit:

   ```
   CL-0010,2026-10-21,sources,0.1.0,0.2.0,your-name,Enabled NHS Jobs XML endpoint,Endpoint copied from NHSBSA Self-Serve Job Adverts API v1.07
   ```

9. Run the workflow (step 1.6). In `logs/runs.csv`, the `collect_nhsjobs`
   row should now say `OK` with a record count. If it says `BLOCKED` or
   `ERROR`, the message column explains why.

Canonical field names you can map in `field_map`:
`source_record_id, url, title, employer, location, postcode, pay_text,
band_text, hours_text, contract_text, working_pattern_text,
closing_date_text, posted_date_text, staff_group_text, description`.
For nested elements use dots, e.g. `salary.text`.

### 2.2 Scotland and Northern Ireland

No official machine-readable feed documentation has been confirmed for the
NHS Scotland National Recruitment Portal (`apply.jobs.scot.nhs.uk`) or HSC
jobs (`jobs.hscni.net`). Before enabling either:

1. Look for an official RSS/XML/JSON feed or API page on the site, or write
   to the site owner asking whether one exists and whether automated
   collection is permitted.
2. Only when you have that documentation, follow the steps in 2.1.

The code does not scrape web pages. If no feed exists, a page parser would
need to be written, reviewed and versioned as a separate change.

---

## Part 3 – Changing rules, prompts or code (versioning)

Whenever you edit `taxonomy.yaml`, `ai_lexicon.yaml`, `sources.yaml`,
`pipeline.yaml`, any file in `prompts/`, or any file in `scripts/`:

1. Make the edit.
2. Increase that artefact's version: the `version:` line in YAML files, the
   `version:` line at the top of prompt files, or `PIPELINE_VERSION` in
   `scripts/common.py` for any script change.
3. Add one new line at the bottom of `logs/change_log.csv` with a new
   `change_id`, the artefact name, old and new version, your name, what
   changed and why.

Artefact names: `taxonomy`, `ai_lexicon`, `sources`, `pipeline_config`,
`code`, `prompt_extraction`, `prompt_classification`,
`prompt_verification`, `prompt_change_detection`.

If you forget, the next run fails validation and the message tells you
which file needs a version bump. Versions are recorded in every output, so
results produced under different rules can always be told apart.

---

## Part 4 – Switching on GenAI (optional, costs money)

The rule-based layer finds *where* AI is mentioned. The GenAI layer
interprets *what the mention means for the role*, with quotes and
confidence. It is capped at `max_items_per_run` adverts per day.

1. Create an API key in the Anthropic developer console (docs.claude.com links to it).
2. In GitHub: **Settings > Secrets and variables > Actions > New repository
   secret**. Name: `ANTHROPIC_API_KEY`. Secret: paste the key. Click
   **Add secret**.
3. Open the official models page at docs.claude.com and copy an exact model
   identifier.
4. Edit `config/pipeline.yaml`: set `genai: enabled: true`, paste the model
   identifier into `model:`, and bump `version:`.
5. Add a `pipeline_config` row to `logs/change_log.csv`.

Outputs appear in `data/derived/genai_runs/`. Records with status
`ACCEPTED` passed every deterministic check; all others are kept for audit
but excluded from analysis.

---

## Part 5 – Reading the outputs

* **Dashboard:** your GitHub Pages address.
* `data/derived/changes/indicators.jsonl`: month × nation × role family,
  share of adverts with AI content in the role text, with 95% intervals.
  Cells with fewer than 30 adverts are withheld.
* `data/derived/changes/change_signals.jsonl`: months that differ from
  their baseline once uncertainty is taken into account.
* `data/derived/changes/role_change_candidates.jsonl`: successive adverts
  for the same job, with added and removed sentences.
* `data/derived/changes/term_emergence.jsonl`: when each AI term first
  appeared.
* **Intermediate files** (normalised records, clusters, rule
  classifications) are rebuilt every day from the stored observations and
  are not kept in the repository. To inspect them: **Actions** tab, open a
  run, scroll to **Artifacts**, download `derived-...`.

---

## Method notes and limitations

**Adverts are not jobs.** An advert can cover several posts, a waiting list
or a bank pool; many posts are filled without public adverts. Counts here
measure advertised demand only. Official vacancy and workforce statistics
are listed as `stock` sources and must never be added to advert counts.

**Coverage is uneven.** NHS Jobs covers most English and Welsh NHS
employers. GP practices, social enterprises and independent providers often
advertise elsewhere, so primary care and independent-sector roles will be
under-represented until further sources are added.

**Absence of evidence.** An advert that does not mention AI may still
involve AI. `AI_NONE_OBSERVED` means "not stated", never "not used".

**Boilerplate.** Sentences repeated across adverts for *different* roles at
an employer (or across many employers) are treated as boilerplate. AI
mentioned only there is `AI_ORGANISATIONAL_CONTEXT`, not role content.

**Re-advert pairs.** Successive adverts are linked by employer and title to
find candidates for comparison. That link is not a classification; whether
it is the same role is decided from the duties (by review or GenAI).

**Nation.** Postcode areas that cross a border (CH, HR, LD, SY, TD) are not
used to assign nation. Disagreeing evidence gives `UNKNOWN`. Pay and terms
differ by nation, so analysis is always split by nation.

**Privacy.** E-mail addresses and phone numbers are removed before anything
is stored; the SHA-256 of the original is kept for integrity. Names of
recruiting contacts may remain in advert text. Do not publish individual
adverts' contact details, and review the terms of use of each source.

**Storage.** Unchanged adverts are stored once; later sightings are small
pointer records. Validation warns when stored observations exceed 500 MB.
At that point move observations to external storage before the repository
becomes too large for GitHub.

**Ten-year view.** Daily collection cannot see the past. Historical
evidence comes from archived snapshots (National Records of Scotland web
archive, Internet Archive) and published workforce statistics. Archive
snapshots are partial, biased samples; they are kept as `context` sources,
analysed separately, and never merged into daily counts. The lexicon tracks
older technologies (speech recognition, digital dictation) so that current
AI adoption can be compared with earlier waves of automation.

**Provider register.** `providers.csv` contains a seed list of Scottish,
Welsh and Northern Irish bodies marked `UNVERIFIED`. Confirm each against
the NHS Organisation Data Service (ODS) and change its status to
`VERIFIED` with the source URL. English trusts, GP practices and other
providers can be added the same way.

---

## Troubleshooting

* **Red cross on the Actions tab after editing a file.** Open the run and
  click the failed step. The most common cause is a changed file without a
  version bump and change-log row (see Part 3).
* **Daily run stopped happening.** GitHub pauses scheduled workflows in
  public repositories after 60 days with no activity. Run it manually once
  from the Actions tab.
* **Dashboard says no data.** Check that Pages uses `main` and `/docs`, and
  that at least one daily run has finished successfully.
