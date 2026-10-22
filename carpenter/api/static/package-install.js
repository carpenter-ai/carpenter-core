/* Package install approval page logic.
 *
 * The page is a decision surface, not an input surface: the only things
 * this script reads are the per-package grant checkboxes.  Destination,
 * package name, source spec and tree hash are never sent — the server
 * takes all of those from the recorded request.
 *
 * Deciding needs platform authentication as well as the link: UI_TOKEN
 * is substituted in only when the viewer opened this page with a valid
 * platform token, so an approval cannot be made by whoever holds the
 * link alone.  Without it the POST is refused with 401, which the
 * error path below reports.
 */
var UI_TOKEN = "__UI_TOKEN__";
function _collectGrants() {
    var grants = {};
    var boxes = document.querySelectorAll('input[data-pkg]');
    for (var i = 0; i < boxes.length; i++) {
        var box = boxes[i];
        var pkg = box.getAttribute('data-pkg');
        var field = box.getAttribute('data-grant');
        if (!grants[pkg]) {
            grants[pkg] = {capabilities: false, write_chat_tools: false};
        }
        grants[pkg][field] = !!box.checked;
    }
    return grants;
}

function _disableButtons() {
    var btns = document.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) {
        btns[i].disabled = true;
    }
}

function decide(decision) {
    var payload = {decision: decision};
    if (decision === 'approve') {
        payload.grants = _collectGrants();
    }
    _disableButtons();
    var hdrs = {'Content-Type': 'application/json'};
    if (UI_TOKEN) { hdrs['Authorization'] = 'Bearer ' + UI_TOKEN; }
    fetch('__DECIDE_URL__', {
        method: 'POST',
        headers: hdrs,
        body: JSON.stringify(payload)
    }).then(function(r) {
        return r.json().then(function(data) {
            if (r.status === 401) {
                throw new Error(
                    'Not signed in to the platform. Open this page from the '
                    + 'Carpenter web UI (or with your platform token) to '
                    + 'approve or reject. Nothing was changed.');
            }
            if (!r.ok) { throw new Error(data.detail || 'Request failed'); }
            return data;
        });
    }).then(function(data) {
        var colors = {approve: '#a6e22e', reject: '#f92672'};
        var extra = (decision === 'approve')
            ? '<p>The install is queued and runs on the platform. '
              + 'Reload this page to see how it went.</p>'
            : '<p>Nothing was installed and the quarantined copy has been deleted.</p>';
        document.body.innerHTML =
            '<div style="padding:40px;text-align:center;font-family:monospace;">'
            + '<h2 style="color:' + (colors[decision] || '#f8f8f2') + '">'
            + decision.toUpperCase() + '</h2>'
            + '<p>Decision recorded.</p>' + extra + '</div>';
    }).catch(function(err) {
        alert('Error: ' + err.message);
        var btns = document.querySelectorAll('button');
        for (var i = 0; i < btns.length; i++) { btns[i].disabled = false; }
    });
}
