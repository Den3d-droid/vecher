import urllib.request, urllib.error, json
K="AIzaSyAiIwqMMqQUbe2w8Wfk6mOnOc1rzvWf3Uo"
def get(u, hdr={}):
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=hdr), timeout=30) as r: return r.status, r.read().decode()[:800]
    except urllib.error.HTTPError as e: return e.code, e.read().decode()[:800]
print("AUTHCFG", get("https://identitytoolkit.googleapis.com/v1/projects?key="+K, {"Referer":"https://den3d-droid.github.io/"}))
print("FS_NOAUTH", get("https://firestore.googleapis.com/v1/projects/vecher-a2124/databases/(default)/documents/users/test?key="+K))
print("PAGE", get("https://den3d-droid.github.io/vecher/firebase-config.js")[0])
