import sys

b64 = open('logo_b64.txt').read().strip()
html = open('templates/base.html').read()
old = "{{ url_for('static', filename='assets/goodgurlgglogo.png') }}"
if old not in html:
    print("ERROR: target string not found in base.html")
    sys.exit(1)
html = html.replace(old, b64)
open('templates/base.html', 'w').write(html)
print("Done - logo inlined as base64 data URI")
