from __future__ import annotations

import json
import sys
from urllib.error import URLError

from confluence_client import ConfluenceAPIError, ConfluenceClient


def main() -> int:
    try:
        client = ConfluenceClient.from_env()
        user = client.get_current_user()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2
    except ConfluenceAPIError as error:
        print(error, file=sys.stderr)
        if error.status_code in (401, 403):
            print(
                "Check CONFLUENCE_USER and CONFLUENCE_API_TOKEN in .env.",
                file=sys.stderr,
            )
        return 1
    except URLError as error:
        print(f"Could not reach Atlassian API: {error.reason}", file=sys.stderr)
        return 1
    except TimeoutError:
        print("Atlassian API request timed out.", file=sys.stderr)
        return 1
    except json.JSONDecodeError:
        print("Atlassian API returned a non-JSON response.", file=sys.stderr)
        return 1

    display_name = user.get("displayName") or user.get("publicName") or "unknown user"
    account_id = user.get("accountId", "unknown account")

    print("Atlassian API connection successful.")
    print(f"Authenticated as: {display_name} ({account_id})")
    print(f"Confluence URL: {client.config.base_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
