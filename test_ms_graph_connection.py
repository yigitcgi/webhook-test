from __future__ import annotations

import json
import sys
from urllib.error import URLError

from ms_graph_client import MSGraphAPIError, MSGraphClient


def main() -> int:
    try:
        client = MSGraphClient.from_env()
        health = client.health_check()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2
    except MSGraphAPIError as error:
        print(error, file=sys.stderr)
        if error.status_code in (401, 403):
            print(
                "Check MS_GRAPH_ACCESS_TOKEN in .env and confirm the token has required Graph scopes.",
                file=sys.stderr,
            )
        if error.response_body:
            print(error.response_body[:1000], file=sys.stderr)
        return 1
    except URLError as error:
        print(f"Could not reach Microsoft Graph API: {error.reason}", file=sys.stderr)
        return 1
    except TimeoutError:
        print("Microsoft Graph API request timed out.", file=sys.stderr)
        return 1
    except json.JSONDecodeError:
        print("Microsoft Graph API returned a non-JSON response.", file=sys.stderr)
        return 1

    user = health["user"]
    display_name = user.get("display_name") or "unknown user"
    principal_name = user.get("user_principal_name") or user.get("mail") or "unknown principal"

    print("Microsoft Graph API connection successful.")
    print(f"Authenticated as: {display_name} ({principal_name})")
    print(f"Graph URL: {health['base_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
