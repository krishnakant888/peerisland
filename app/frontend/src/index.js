'use strict';

const express = require('express');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.static(path.join(__dirname, '..', 'public')));

app.get('/health', (_req, res) => {
  res.json({ status: 'healthy', service: 'peerisland-frontend', timestamp: new Date().toISOString() });
});

app.get('*', (_req, res) => {
  res.sendFile(path.join(__dirname, '..', 'public', 'index.html'));
});

app.listen(PORT, () => console.log(`Frontend listening on :${PORT}`));
