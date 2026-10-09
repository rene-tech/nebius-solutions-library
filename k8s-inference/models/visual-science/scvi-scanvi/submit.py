"""Upload AnnData/reference inputs and submit a durable scVI/scANVI operation.

Requires Python 3.11+ and httpx. Uses SCIENTIFIC_MODELS_API_KEY, never a key in
command arguments. The same streaming transport is exercised by internal QA.
"""

from qualify_api import main


if __name__ == "__main__":
    main(require_qa=False)
