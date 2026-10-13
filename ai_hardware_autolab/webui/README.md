# Web interface

Build frontend/ with npm ci and npm run build. Start the daemon and local web server
using scripts/run-platform.ps1 from the repository root as described in ../README.md.

Use the complete session URL printed by the local server. Authentication and local artifact
path checks protect project files. The UI provides project selection, model settings,
role conversations, feedback, job results and detailed project knowledge maps.

cloudflare/ provides optional remote hosting and knowledge replication. Configure each
installation's account, device token and operator authentication independently.
assets/ contains the vendored office art and its source/license manifest.
