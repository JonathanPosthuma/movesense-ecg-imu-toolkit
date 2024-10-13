// SimpleQueue.cpp

#include "SimpleQueue.h"

SimpleQueue::SimpleQueue()
    : mFront(0), mRear(0), mCount(0)
{
}

void SimpleQueue::enqueue(uint32_t value)
{
    if (mCount < MAX_QUEUE_SIZE)
    {
        mData[mRear] = value;
        mRear = (mRear + 1) % MAX_QUEUE_SIZE;
        mCount++;
    }
    // Else, queue is full; handle overflow if needed
}

uint32_t SimpleQueue::dequeue()
{
    if (mCount > 0)
    {
        uint32_t value = mData[mFront];
        mFront = (mFront + 1) % MAX_QUEUE_SIZE;
        mCount--;
        return value;
    }
    // Else, queue is empty; handle underflow if needed
    return 0;  // Return 0 or an invalid value to indicate empty queue
}

bool SimpleQueue::isEmpty() const
{
    return (mCount == 0);
}

void SimpleQueue::clear()
{
    mFront = 0;
    mRear = 0;
    mCount = 0;
}
