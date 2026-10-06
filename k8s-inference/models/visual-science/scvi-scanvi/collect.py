"""Poll a durable scVI/scANVI operation and download/hash-check its outputs.

Uses SCIENTIFIC_MODELS_API_KEY. A timeout does not cancel the remote operation;
invoke again with the same --operation-id. REST is the default, MCP optional.
"""

from qualify_hosted import main


if __name__ == "__main__":
    main(require_qa=False)
