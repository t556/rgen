(() => {
    const job = document.getElementById('job');
    if (!job || ['complete', 'failed'].includes(job.dataset.status)) return;
    const update = async () => {
        try {
            const response = await fetch(job.dataset.statusUrl, {cache: 'no-store'});
            if (!response.ok) throw new Error('Job status unavailable. The server may have restarted.');
            const state = await response.json();
            document.getElementById('status').textContent = state.status;
            document.getElementById('runs').textContent = `${state.runs_done} / ${state.runs_total} runs`;
            document.getElementById('elapsed').textContent = `${state.elapsed.toFixed(1)}s`;
            document.getElementById('current-run').textContent = state.current_run ?? '—';
            const progress = document.getElementById('progress');
            progress.max = state.runs_total || 1;
            progress.value = state.runs_done;
            document.getElementById('log').textContent = state.log || 'Waiting for the job runner…';
            if (['complete', 'failed'].includes(state.status)) {
                window.location.reload();
                return;
            }
        } catch (error) {
            document.getElementById('status').textContent = error.message;
        }
        window.setTimeout(update, 600);
    };
    update();
})();
