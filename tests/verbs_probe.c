/* Standard verbs observation; loaded only in test rank processes. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <infiniband/verbs.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

struct call_stats {
    uint64_t application_tid, contexts, live_contexts;
    uint64_t post_calls, posted_wrs, application_posts, last_post_tid;
    uint64_t poll_calls, held_polls, application_polls;
    uint64_t completions, application_completions, last_completion_tid;
    uint64_t shutdown_releases;
};

/* RC WRITE measurements; other operations do not contribute to these counts. */
struct write_stats {
    uint64_t posted, completed, signaled, cqes;
    uint64_t in_flight, peak_in_flight, post_batch, poll_entries;
    uint64_t post_errors, completion_errors, capacity_errors, untracked_writes;
};

struct context_hooks {
    struct ibv_context *context;
    int (*post_send)(struct ibv_qp *, struct ibv_send_wr *, struct ibv_send_wr **);
    int (*poll_cq)(struct ibv_cq *, int, struct ibv_wc *);
    struct context_hooks *next;
};

struct signaled_wr {
    uint64_t wr_id, write_sequence;
    struct signaled_wr *next;
};

struct qp_state {
    struct ibv_qp *qp;
    uint32_t send_capacity;
    bool signal_all;
    uint64_t posted, completed;
    struct signaled_wr *pending_head;
    struct signaled_wr *pending_tail;
    struct qp_state *next;
};

static struct {
    pthread_mutex_t lock;
    struct context_hooks *contexts;
    struct qp_state *queue_pairs;
    struct call_stats calls;
    struct write_stats writes;
    bool track_writes, pause_completions;
    struct ibv_qp *paused_qp;
} probe = {.lock = PTHREAD_MUTEX_INITIALIZER};

static void *resolve_next(const char *name) {
    void *value = dlsym(RTLD_NEXT, name);
    if (!value) {
        fprintf(stderr, "verbs probe: %s: %s\n", name, dlerror());
        abort();
    }
    return value;
}

static void *allocate_record(size_t size) {
    void *record = calloc(1, size);
    if (!record) {
        fprintf(stderr, "verbs probe: cannot allocate observation record\n");
        abort();
    }
    return record;
}

/* Helpers access shared records with probe.lock held. */
static struct context_hooks *find_context(struct ibv_context *context) {
    for (struct context_hooks *hooks = probe.contexts; hooks; hooks = hooks->next)
        if (hooks->context == context)
            return hooks;
    fprintf(stderr, "verbs probe: device context was not observed\n");
    abort();
}

static struct qp_state *find_qp(struct ibv_qp *qp) {
    for (struct qp_state *state = probe.queue_pairs; state; state = state->next)
        if (state->qp == qp)
            return state;
    return NULL;
}

static void reset_qp_tracking(struct qp_state *state) {
    while (state->pending_head) {
        struct signaled_wr *completion = state->pending_head;
        state->pending_head = completion->next;
        free(completion);
    }
    state->pending_tail = NULL;
    state->posted = state->completed = 0;
}

/* Test controls. Begin a measurement only when previous requests have finished. */
void r101_probe_observe_writes(int enabled) {
    pthread_mutex_lock(&probe.lock);
    for (struct qp_state *state = probe.queue_pairs; state; state = state->next)
        reset_qp_tracking(state);
    if (enabled)
        memset(&probe.writes, 0, sizeof(probe.writes));
    probe.track_writes = enabled != 0;
    pthread_mutex_unlock(&probe.lock);
}

void r101_probe_get_write_stats(struct write_stats *output) {
    pthread_mutex_lock(&probe.lock);
    *output = probe.writes;
    pthread_mutex_unlock(&probe.lock);
}

void r101_probe_hold_completions(int paused) {
    pthread_mutex_lock(&probe.lock);
    probe.calls.application_tid = (uint64_t)syscall(SYS_gettid);
    probe.pause_completions = paused != 0;
    probe.paused_qp = NULL;
    pthread_mutex_unlock(&probe.lock);
}

void r101_probe_get_call_stats(struct call_stats *output) {
    pthread_mutex_lock(&probe.lock);
    *output = probe.calls;
    pthread_mutex_unlock(&probe.lock);
}

static void record_posted_wr(struct ibv_qp *qp, const struct ibv_send_wr *wr) {
    if (qp->qp_type != IBV_QPT_RC)
        return;
    bool is_write = wr->opcode == IBV_WR_RDMA_WRITE;
    struct qp_state *state = find_qp(qp);
    struct write_stats *stats = &probe.writes;
    if (!state) {
        if (is_write)
            ++stats->untracked_writes;
        return;
    }
    if (is_write) {
        ++state->posted;
        ++stats->posted;
        ++stats->in_flight;
        if (stats->in_flight > stats->peak_in_flight)
            stats->peak_in_flight = stats->in_flight;
        if (state->posted - state->completed > state->send_capacity)
            ++stats->capacity_errors;
    }
    if (!state->signal_all && !(wr->send_flags & IBV_SEND_SIGNALED))
        return;

    /* Keep every signaled SQ entry to distinguish mixed opcodes and reused IDs. */
    struct signaled_wr *completion = allocate_record(sizeof(*completion));
    completion->wr_id = wr->wr_id;
    completion->write_sequence = state->posted;
    if (state->pending_tail)
        state->pending_tail->next = completion;
    else
        state->pending_head = completion;
    state->pending_tail = completion;
    if (is_write)
        ++stats->signaled;
}

static void record_send_completion(struct ibv_cq *cq, const struct ibv_wc *wc) {
    struct write_stats *stats = &probe.writes;
    struct qp_state *state = probe.queue_pairs;
    while (state && (state->qp->send_cq != cq || state->qp->qp_num != wc->qp_num))
        state = state->next;
    if (!state || state->qp->qp_type != IBV_QPT_RC)
        return;
    /* An error CQE has no valid opcode. Keep errors affecting pending WRITEs visible. */
    if (wc->status != IBV_WC_SUCCESS) {
        if (state->posted != state->completed)
            ++stats->completion_errors;
        return;
    }
    if (wc->opcode & IBV_WC_RECV)
        return;
    if (!state->pending_head || state->pending_head->wr_id != wc->wr_id) {
        ++stats->completion_errors;
        return;
    }
    /* A later SQ completion can cover preceding unsignaled WRITEs as well. */
    struct signaled_wr *completion = state->pending_head;
    uint64_t retired = completion->write_sequence - state->completed;
    state->completed = completion->write_sequence;
    stats->completed += retired;
    stats->in_flight -= retired;
    if (retired)
        ++stats->cqes;
    state->pending_head = completion->next;
    if (!state->pending_head)
        state->pending_tail = NULL;
    free(completion);
}

static int
observed_post_send(struct ibv_qp *qp, struct ibv_send_wr *wr, struct ibv_send_wr **bad_wr) {
    uint64_t tid = (uint64_t)syscall(SYS_gettid);
    pthread_mutex_lock(&probe.lock);
    struct context_hooks *hooks = find_context(qp->context);
    struct call_stats *stats = &probe.calls;
    ++stats->post_calls;
    stats->last_post_tid = tid;
    if (probe.pause_completions && !probe.paused_qp)
        probe.paused_qp = qp;
    if (tid == stats->application_tid) {
        if (stats->application_posts == 0)
            fprintf(
                stderr, "verbs probe: post_send on application thread %lu\n", (unsigned long)tid);
        ++stats->application_posts;
    }
    int result = hooks->post_send(qp, wr, bad_wr);
    uint64_t accepted = 0;
    uint64_t writes_before = probe.writes.posted;
    for (struct ibv_send_wr *request = wr; request; request = request->next) {
        if (result && (!bad_wr || !*bad_wr || request == *bad_wr))
            break;
        ++accepted;
        if (probe.track_writes)
            record_posted_wr(qp, request);
    }
    stats->posted_wrs += accepted;
    if (probe.track_writes) {
        if (result && qp->qp_type == IBV_QPT_RC && bad_wr && *bad_wr &&
            (*bad_wr)->opcode == IBV_WR_RDMA_WRITE)
            ++probe.writes.post_errors;
        uint64_t accepted_writes = probe.writes.posted - writes_before;
        if (accepted_writes > probe.writes.post_batch)
            probe.writes.post_batch = accepted_writes;
    }
    pthread_mutex_unlock(&probe.lock);
    return result;
}

static int observed_poll_cq(struct ibv_cq *cq, int count, struct ibv_wc *wc) {
    uint64_t tid = (uint64_t)syscall(SYS_gettid);
    pthread_mutex_lock(&probe.lock);
    struct context_hooks *hooks = find_context(cq->context);
    struct call_stats *stats = &probe.calls;
    ++stats->poll_calls;
    if (tid == stats->application_tid) {
        if (stats->application_polls == 0)
            fprintf(stderr, "verbs probe: poll_cq on application thread %lu\n", (unsigned long)tid);
        ++stats->application_polls;
    }
    if (probe.pause_completions) {
        ++stats->held_polls;
        pthread_mutex_unlock(&probe.lock);
        return 0;
    }
    /* Holding the lock also lets a pause wait for active provider polls. */
    int result = hooks->poll_cq(cq, count, wc);
    if (probe.track_writes) {
        if (probe.writes.in_flight > 0 && count > 0 && (uint64_t)count > probe.writes.poll_entries)
            probe.writes.poll_entries = (uint64_t)count;
        if (result < 0 && probe.writes.in_flight > 0)
            ++probe.writes.completion_errors;
        for (int i = 0; i < result; ++i)
            record_send_completion(cq, &wc[i]);
    }
    if (result > 0) {
        stats->completions += (uint64_t)result;
        stats->last_completion_tid = tid;
        if (tid == stats->application_tid)
            stats->application_completions += (uint64_t)result;
    }
    pthread_mutex_unlock(&probe.lock);
    return result;
}

struct ibv_context *ibv_open_device(struct ibv_device *device) {
    struct ibv_context *(*call)(struct ibv_device *) = resolve_next("ibv_open_device");
    struct ibv_context *context = call(device);
    if (!context)
        return NULL;
    struct context_hooks *hooks = allocate_record(sizeof(*hooks));
    hooks->context = context;
    hooks->post_send = context->ops.post_send;
    hooks->poll_cq = context->ops.poll_cq;
    pthread_mutex_lock(&probe.lock);
    hooks->next = probe.contexts;
    probe.contexts = hooks;
    context->ops.post_send = observed_post_send;
    context->ops.poll_cq = observed_poll_cq;
    ++probe.calls.contexts;
    ++probe.calls.live_contexts;
    pthread_mutex_unlock(&probe.lock);
    return context;
}

struct ibv_qp *ibv_create_qp(struct ibv_pd *pd, struct ibv_qp_init_attr *attr) {
    struct ibv_qp *(*call)(struct ibv_pd *, struct ibv_qp_init_attr *) =
        resolve_next("ibv_create_qp");
    struct ibv_qp *qp = call(pd, attr);
    if (!qp)
        return NULL;
    struct qp_state *state = allocate_record(sizeof(*state));
    state->qp = qp;
    state->send_capacity = attr->cap.max_send_wr;
    state->signal_all = attr->sq_sig_all != 0;
    pthread_mutex_lock(&probe.lock);
    state->next = probe.queue_pairs;
    probe.queue_pairs = state;
    pthread_mutex_unlock(&probe.lock);
    return qp;
}

/* Closing QPs must still be able to drain their completions. */
static void resume_for_shutdown(struct ibv_qp *qp) {
    pthread_mutex_lock(&probe.lock);
    if (probe.pause_completions && probe.paused_qp == qp) {
        probe.pause_completions = false;
        probe.paused_qp = NULL;
        ++probe.calls.shutdown_releases;
    }
    pthread_mutex_unlock(&probe.lock);
}

int ibv_modify_qp(struct ibv_qp *qp, struct ibv_qp_attr *attr, int mask) {
    int (*call)(struct ibv_qp *, struct ibv_qp_attr *, int) = resolve_next("ibv_modify_qp");
    int result = call(qp, attr, mask);
    if (result == 0 && (mask & IBV_QP_STATE) &&
        (attr->qp_state == IBV_QPS_ERR || attr->qp_state == IBV_QPS_RESET))
        resume_for_shutdown(qp);
    return result;
}

int ibv_destroy_qp(struct ibv_qp *qp) {
    int (*call)(struct ibv_qp *) = resolve_next("ibv_destroy_qp");
    resume_for_shutdown(qp);
    pthread_mutex_lock(&probe.lock);
    int result = call(qp);
    if (result == 0) {
        struct qp_state **link = &probe.queue_pairs;
        while (*link && (*link)->qp != qp)
            link = &(*link)->next;
        if (*link) {
            struct qp_state *state = *link;
            if (probe.track_writes)
                probe.writes.in_flight -= state->posted - state->completed;
            reset_qp_tracking(state);
            *link = state->next;
            free(state);
        }
    }
    pthread_mutex_unlock(&probe.lock);
    return result;
}

int ibv_close_device(struct ibv_context *context) {
    int (*call)(struct ibv_context *) = resolve_next("ibv_close_device");
    pthread_mutex_lock(&probe.lock);
    struct context_hooks *hooks = find_context(context);
    context->ops.post_send = hooks->post_send;
    context->ops.poll_cq = hooks->poll_cq;
    int result = call(context);
    if (result == 0) {
        struct context_hooks **link = &probe.contexts;
        while (*link != hooks)
            link = &(*link)->next;
        *link = hooks->next;
        --probe.calls.live_contexts;
        free(hooks);
    } else {
        context->ops.post_send = observed_post_send;
        context->ops.poll_cq = observed_poll_cq;
    }
    pthread_mutex_unlock(&probe.lock);
    return result;
}

/* rdma-mummy-sys resolves verbs through a handle, bypassing plain LD_PRELOAD.
 * libibverbs is already a dependency of this probe. For this one library name,
 * resolve through the global scope, which includes both our hooks and real verbs.
 * Other dlopen calls retain their original behavior. */
void *dlopen(const char *filename, int flags) {
    void *(*call)(const char *, int) = resolve_next("dlopen");
    if (filename && strcmp(filename, "libibverbs.so.1") == 0)
        return call(NULL, flags);
    return call(filename, flags);
}
