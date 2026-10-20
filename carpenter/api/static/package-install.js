/* Package install approval page logic.
 *
 * The page is a decision surface, not an input surface: the only things
 * this script reads are the per-package grant checkboxes.  Destination,
 * package name, source spec and tree hash are never sent — the server
 * takes all of those from the recorded request.
 */
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
    fetch('__DECIDE_URL__', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    }).then(function(r) {
        return r.json().then(function(data) {
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
