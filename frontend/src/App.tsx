import React from 'react';

export default function App() {
  return (
    <div className="min-h-screen bg-slate-900 text-slate-100 flex flex-col items-center justify-center p-8 text-center">
      <div className="max-w-xl">
        <h1 className="text-5xl font-extrabold mb-4 bg-gradient-to-r from-sky-400 to-indigo-400 bg-clip-text text-transparent">
          AutoKT
        </h1>
        <p className="text-xl text-slate-400 mb-8">
          AI-Powered Knowledge-Transfer Platform for Automated Codebase Ingestion, Architectural Graphing, and Developer Onboarding.
        </p>
        <div className="inline-block px-6 py-3 rounded-lg bg-slate-800 border border-slate-700 text-sky-400 text-sm font-medium">
          Vite + React Scaffold Active • Plain SPA
        </div>
      </div>
    </div>
  );
}
