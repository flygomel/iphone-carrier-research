#!/usr/bin/env python3
"""Launch the country-only experiment. Legacy workflows live in earlier tags."""
import sys
import country

if __name__ == '__main__':
    try:
        raise SystemExit(country.main())
    except Exception as error:
        print('Остановлено: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
