# NOTICE — Attribution and Citations

## ClassyFire Classification System

ClassyFire is a web-based application for automated structural classification
of chemical entities. When using ClassyFire classifications, please cite:

  Djoumbou Feunang Y, Eisner R, Knox C, Chepelev L, Hastings J, Owen G,
  Fahy E, Steinbeck C, Wishart DS.
  ClassyFire: automated chemical classification with a comprehensive,
  computable taxonomy.
  J Cheminformatics. 2016;8:61.
  DOI: 10.1186/s13321-016-0174-y

## ChemOnt Taxonomy

The ChemOnt chemical ontology is developed and maintained by the Wishart Lab:
  http://classyfire.wishartlab.com/tax_nodes/C0000000

## Data Storage Notice (FR-009.4)

Structures submitted to this service may be forwarded to the Wishart Lab
ClassyFire web service (https://classyfire.wishartlab.com) for classification.
Successful classification results and identifiers (InChIKeys) are stored
locally in this service's database for caching purposes.

Users should not submit confidential or proprietary structures if forwarding to
the Wishart Lab service is a concern.  Remote classification can be disabled
per-job using the "remote_enabled=false" API parameter or by unchecking the
"Enable remote classification" toggle on the submit page.

## Third-Party Software

This application uses the following open-source libraries and tools:

- **Django** — BSD License — https://www.djangoproject.com/
- **RDKit** — BSD License — https://www.rdkit.org/
- **PostgreSQL** — PostgreSQL License — https://www.postgresql.org/
- **Caddy** — Apache License 2.0 — https://caddyserver.com/
- **ngrok** — ngrok Terms of Service — https://ngrok.com/tos

Full dependency list is available in `pyproject.toml`.
