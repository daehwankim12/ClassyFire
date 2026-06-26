# User Guide

This guide explains how to use the ClassyFire Batch Cache Web application to
classify chemical compounds.

---

## Overview

ClassyFire Batch Cache Web accepts lists of chemical identifiers (SMILES, InChI,
or InChIKey), looks up their chemical classification in the local cache, and
optionally forwards unresolved compounds to the Wishart ClassyFire service.

**Important — data storage notice (FR-009.4):**
Structures submitted via this service may be sent to the Wishart Lab ClassyFire
web service (http://classyfire.wishartlab.com) for classification.  Successful
classification results and identifiers (InChIKeys) are stored locally in the
service's database for caching.  Do not submit confidential structures if this
is a concern.

---

## Submitting a Batch Job

### 1. Open the submit page

Navigate to the root URL of the application (e.g. `http://localhost:8000/`).

### 2. Enter your compounds

Paste one compound per line into the text box.  Accepted formats:

| Format | Example |
|---|---|
| SMILES | `CC(=O)Oc1ccccc1C(=O)O` |
| InChI | `InChI=1S/C9H8O4/c1-6(10)...` |
| InChIKey | `BSYNRYMUTXBXSQ-UHFFFAOYSA-N` |

You can mix all three formats in a single submission.  The order of your input
is preserved in the results (T-01).

**Duplicate rows** are preserved.  If you submit the same compound twice, you
will see two result rows (one for each input row).

### 3. Remote classification toggle

Check **Enable remote classification** to allow the service to forward
unresolved compounds to the Wishart ClassyFire API.  If unchecked, only locally
cached results are returned.

### 4. Submit

Click **Submit**.  Local processing runs immediately.  You are redirected to
the job results page.

---

## Reading Results

The results page shows a table with one row per input compound:

| Column | Description |
|---|---|
| Row | Original row number (1-based). |
| External ID | Your identifier if supplied via CSV upload. |
| Input | The raw string you submitted. |
| Type | Detected type: SMILES / INCHI / INCHIKEY. |
| InChIKey | Canonical InChIKey computed by RDKit. |
| Status | Current classification status. |
| Message | Additional information (e.g. error detail). |

### Status values

| Status | Meaning |
|---|---|
| FOUND_LIVE_CACHE | Classified from a recent Wishart result stored locally. |
| FOUND_SEED_CACHE | Classified from the pre-loaded seed dataset. |
| COMPLETE_AFTER_WISHART | Classified after a successful remote call in this job. |
| FOUND_WISHART_ENTITY | Classified from a Wishart entity lookup. |
| PENDING_CLASSIFICATION | Queued for remote classification (check back later). |
| NEED_STRUCTURE | An InChIKey was submitted without a structure; see below. |
| REMOTE_DELAYED | Remote call deferred (circuit open or retry scheduled). |
| INVALID_INPUT | Could not parse the input as a valid chemical identifier. |

### Filtering and searching

Use the **Status filter** drop-down to show only rows with a specific status
group (FOUND, PENDING, NEED_STRUCTURE, INVALID, DELAYED).

Use the **Search** box to filter rows by the original input or external ID.

### Pagination

Results are paginated (100 rows per page by default).  Use the page navigation
links to browse large jobs.

---

## Completing NEED_STRUCTURE Items

If you submitted an InChIKey that is not in the local cache, the item will
show `NEED_STRUCTURE`.  Classification requires a structure (SMILES or InChI)
to be provided.

### Via the web form

1. Click the **Provide structure** link on the NEED_STRUCTURE row.
2. Enter the SMILES or InChI string for the compound.
3. Click **Submit structure**.

The service will:
1. Compute the InChIKey from the submitted structure using RDKit.
2. Verify it matches the expected InChIKey (mismatch guard T-06).
3. Look up the classification or queue a remote call.

If the computed InChIKey does not match, an error is shown and the item remains
`NEED_STRUCTURE` so you can try again with the correct structure.

### Via the API

```bash
curl -X POST /api/items/<item_id>/structure \
  -H "Content-Type: application/json" \
  -H "X-Job-Secret: <your_secret>" \
  -d '{"structure": "N[C@@H](Cc1c[nH]c2ccccc12)C(=O)O"}'
```

Returns HTTP 200 on success, HTTP 422 on InChIKey mismatch.

---

## Exporting Results

### Supported formats

| Format | MIME type | Notes |
|---|---|---|
| `csv` | `text/csv` | Comma-separated; suitable for Excel. |
| `tsv` | `text/tab-separated-values` | Tab-separated. |
| `json` | `application/json` | Array of objects. |
| `csv.gz` | `application/gzip` | Gzip-compressed CSV. |
| `tsv.gz` | `application/gzip` | Gzip-compressed TSV. |

### Via the web UI

1. On the results page, select a format from the **Export** drop-down.
2. Click **Download**.

### Via the API

```bash
# Create export
curl -X POST /api/jobs/<job_id>/exports \
  -H "Content-Type: application/json" \
  -H "X-Job-Secret: <your_secret>" \
  -d '{"format": "csv"}'

# Response: {"token": "<token>", "expires_at": "..."}

# Download
curl -O /api/exports/<token>
```

### Export guarantees

- Row order matches the original input order (T-11).
- Duplicate input rows appear as separate export rows (T-11).
- Special characters are escaped to prevent CSV/formula injection (T-11).
- Source/provenance fields (seed vs. live) are included in the export (T-11).

---

## Security Considerations

### Result URLs and the job secret

When you submit a job, you receive a per-job **result secret**.  The result URL
embeds this secret in the path (e.g. `/jobs/<id>/<secret>/`).

**Treat the result URL like a password:**
- Do not share it in public channels.
- Do not post it in bug reports or screenshots.
- The `Referrer-Policy: strict-origin` header is set on result pages, so the
  Referer is trimmed to the origin only — the secret-bearing URL path never
  leaks to third-party resources.

**Preferred approach for API clients:** pass the secret via the `X-Job-Secret`
header rather than the `?secret=` query parameter.  The header is not logged
by most proxies; query parameters often appear in access logs.

### Data forwarding to Wishart (FR-009.4)

If `remote_enabled` is on, unresolved structures are sent to the Wishart Lab
ClassyFire API over HTTPS.  The following data is sent:

- The canonical SMILES string of the compound.
- The computed InChIKey.

Classification results (not the raw structure) are stored in the local database.

### Shared-access password

In production mode the service requires an `Authorization: Bearer <token>`
header (shared-access password).  Contact your administrator for the token.

---

## File Upload

For large batches, upload a CSV or TSV file on the submit page:

- The first column is the chemical identifier (SMILES, InChI, or InChIKey).
- An optional column named `id` or `external_id` provides a user-supplied
  identifier that is preserved in the results and exports.
- SDF files (`.sdf`) are also accepted.

Row limits: up to 100 MB per upload (configurable).

---

## Tips

- **Prefer SMILES or InChI** over bare InChIKeys where possible.  InChIKey-only
  inputs that are not in the local cache require you to supply the structure
  later.
- **Enable remote classification** only if you are comfortable with data being
  sent to Wishart and the associated latency.
- **Download exports promptly** — export tokens expire after a limited time.
- **Reload the results page** if some items are still `PENDING_CLASSIFICATION`;
  the worker updates them asynchronously.
