#pragma once

#include <stdint.h>

class SimpleQueue
{
public:
    SimpleQueue();
    void enqueue(uint32_t value);
    uint32_t dequeue();
    bool isEmpty() const;
    void clear();

private:
    static const int MAX_QUEUE_SIZE = 10;  // Adjust size as needed
    uint32_t mData[MAX_QUEUE_SIZE];
    int mFront;
    int mRear;
    int mCount;
};
