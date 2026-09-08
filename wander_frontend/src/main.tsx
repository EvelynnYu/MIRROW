import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { PersonaProvider } from './persona';
import './panels.css';
import './shell.css';

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><PersonaProvider><App /></PersonaProvider></React.StrictMode>);
