package com.example.demo;

import java.util.ArrayList;
import java.util.List;

public class TaskScheduler {
    private final List<Runnable> tasks = new ArrayList<>();
    private boolean running = false;

    public void addTask(Runnable task) {
        if (task == null) {
            throw new IllegalArgumentException("Task cannot be null");
        }
        tasks.add(task);
    }

    public void runAll() {
        if (running) {
            return;
        }
        running = true;
        try {
            for (Runnable task : tasks) {
                task.run();
            }
        } finally {
            running = false;
            tasks.clear();
        }
    }

    public int pendingCount() {
        return tasks.size();
    }
}
