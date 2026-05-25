/**
 * Manages a queue of tasks and executes them sequentially.
 * 
 * TaskScheduler allows clients to register Runnable tasks and execute them all at once.
 * It prevents concurrent execution through a simple running flag and automatically clears
 * all tasks after execution completes.
 * 
 * New joiners should understand this when implementing task batching or job scheduling
 * patterns. Note that this class is not thread-safe.
 */
package com.example.demo;

import java.util.ArrayList;
import java.util.List;

public class TaskScheduler {
    private final List<Runnable> tasks = new ArrayList<>();
    private boolean running = false;

    /**
     * Registers a task to be executed when runAll() is called.
     * 
     * @param task the Runnable to schedule; must not be null
     * @throws IllegalArgumentException if task is null
     */
    public void addTask(Runnable task) {
        // Prevent null tasks which would cause NPE during execution
        if (task == null) {
            throw new IllegalArgumentException("Task cannot be null");
        }
        tasks.add(task);
    }

    /**
     * Executes all registered tasks sequentially and clears the task queue.
     * 
     * If called while already running, this method returns immediately without action.
     * All tasks are cleared after execution, regardless of success or exception.
     * Not thread-safe: concurrent calls may skip execution.
     */
    public void runAll() {
        // Prevent re-entrant execution: if already running, skip this invocation
        if (running) {
            return;
        }
        running = true;
        try {
            // Execute each task in order
            for (Runnable task : tasks) {
                task.run();
            }
        } finally {
            // Always reset state and clear tasks, even if a task threw an exception
            running = false;
            tasks.clear();
        }
    }

    /**
     * Returns the number of tasks pending execution.
     * 
     * @return the count of registered tasks not yet run
     */
    public int pendingCount() {
        return tasks.size();
    }
}
