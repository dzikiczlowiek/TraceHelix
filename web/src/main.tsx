import { StrictMode } from 'react'; import { createRoot } from 'react-dom/client'; import './style.css';
export function App() { return <main><h1>TraceHelix</h1><p>Local, auditable trace analysis.</p></main>; }
const root = document.getElementById('root'); if (root) createRoot(root).render(<StrictMode><App /></StrictMode>);
