import { createRoot } from 'react-dom/client';
import App from './App';
import './app.css';
import { setupDemo } from './demo';
if(new URLSearchParams(location.search).get('demo')==='1') setupDemo();
createRoot(document.getElementById('root')!).render(<App/>);
