import { useMemo } from 'react'
import { installToken } from './api/token'
import { Shell } from './app/Shell'
import { usePlatform } from './app/usePlatform'

export function App() {
  // Once, before anything else renders: the token is read out of the URL and
  // the URL is cleaned. Doing it in a memo rather than an effect means no
  // render ever sees the credential still in `location.search`.
  const token = useMemo(() => installToken(), [])
  const platform = usePlatform(token)

  if (!token) return <NoToken />
  return <Shell platform={platform} token={token} api={platform.api} />
}

function NoToken() {
  return (
    <div className="pad prose gate">
      <h2>This page needs the session token</h2>
      <p>
        The backend prints a URL with the token in it when it starts, and the token is generated
        fresh each run — so a bookmark from a previous run will land here.
      </p>
      <pre>
        py -3 -m QKD_PROJ.webui.backend.app{'\n'}
        open: http://127.0.0.1:8787/?token=…
      </pre>
      <p className="muted">
        It is also written to <code>QKD_PROJ/webui/state/session.json</code>, which is git-ignored.
      </p>
    </div>
  )
}
