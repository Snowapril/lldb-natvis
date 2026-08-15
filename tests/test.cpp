// Test types for lldb-natvis: one type per natvis feature (see sample.natvis).
#include <cstdio>
#include <cstdlib>
#include <cstring>

// --- wildcard + $T1 + ArrayItems --------------------------------------------
template <typename T>
struct MyVector {
    T*  mData;
    int mSize;
    int mCapacity;
    void push(T v) {
        if (mSize == mCapacity) {
            mCapacity = mCapacity ? mCapacity * 2 : 4;
            mData = (T*)realloc(mData, sizeof(T) * mCapacity);
        }
        mData[mSize++] = v;
    }
};

// --- fixed + multi-dim arrays, {arr,[n]} spec, Synthetic sub-node -----------
struct FixedArr {
    int data[10];
    int grid[3][4];
    int count;
};

// --- LinkedListItems --------------------------------------------------------
struct Node {
    int   val;
    Node* next;
};
struct MyList {
    Node* head;
    int   count;
};

// --- TreeItems (two-wildcard binding) ---------------------------------------
template <typename K, typename V>
struct MyMap {
    struct TreeNode {
        K         key;
        V         value;
        TreeNode* left;
        TreeNode* right;
    };
    TreeNode* root;
    int       size;

    void insert(K k, V v) {
        TreeNode** slot = &root;
        while (*slot) slot = (k < (*slot)->key) ? &(*slot)->left : &(*slot)->right;
        *slot = new TreeNode{k, v, nullptr, nullptr};
        ++size;
    }
};

// --- strings: ,s ,su + StringView -------------------------------------------
struct MyString {
    const char* data;
    int         len;
};
struct MyU16String {
    const char16_t* data;
    int             len;
};

// --- enums: ,en + flags decomposition ---------------------------------------
enum class Color { Red = 1, Green = 2, Blue = 3 };
enum FlagBits { F_A = 1, F_B = 2, F_C = 4 };
struct ColorHolder {
    Color    c;
    FlagBits f;
};

// --- Intrinsic with Parameter -----------------------------------------------
struct Temperature {
    float celsius;
};

// --- inheritance: Inheritable, ExpandedItem base cast -----------------------
struct Shape {
    int id;
};
struct Circle : Shape {
    float radius;
};
struct Square : Shape {   // no natvis of its own: matched via base walk
    int side;
};

// --- ExpandedItem this-splice -----------------------------------------------
struct SelfExpand {
    int x;
    int y;
};

// --- Optional + DisplayString alternative ordering --------------------------
struct OptHolder {
    int mode;
    int a;
};

// --- Synthetic with computed DisplayString ----------------------------------
struct Vec3 {
    float x, y, z;
};

// --- IndexListItems with $i -------------------------------------------------
struct Deque {
    int ring[8];
    int start;
    int len;
};

// --- CustomListItems (hash table) -------------------------------------------
struct Bucket {
    int     key;
    int     value;
    Bucket* next;
};
struct MyHashTable {
    Bucket* buckets[8];
    int     count;
    void put(int k, int v) {
        Bucket*& slot = buckets[k & 7];
        slot = new Bucket{k, v, slot};
        ++count;
    }
};

// --- Priority resolution (two natvis entries for one type) ------------------
struct Both {
    int v;
};

// --- AlternativeType --------------------------------------------------------
struct AliasVector {
    int v;
};

// --- regression: negative <Size> must yield zero children -------------------
struct BadSize {
    int  n;
    int* p;
};

// --- regression: recursive <Intrinsic> must be bounded (Optional fallback) --
struct RecIntr {
    int v;
};

// --- IncludeView / ExcludeView + ,view(x) -----------------------------------
struct Viewy {
    int a;
    int b;
};
struct ViewHolder {
    Viewy vw;
};

// --- typedef (cascade) ------------------------------------------------------
typedef MyVector<int> IntVecAlias;

int main() {
    MyVector<int> vec{};
    for (int i = 1; i <= 5; ++i) vec.push(i * 10);

    IntVecAlias& aliasRef = vec;

    FixedArr fixed{};
    fixed.count = 6;
    for (int i = 0; i < 10; ++i) fixed.data[i] = i * i;
    for (int r = 0; r < 3; ++r)
        for (int c = 0; c < 4; ++c) fixed.grid[r][c] = r * 10 + c;

    MyList list{};
    Node n3{3, nullptr}, n2{2, &n3}, n1{1, &n2};
    list.head = &n1;
    list.count = 3;

    MyMap<int, int> map{};
    map.insert(3, 300);
    map.insert(1, 100);
    map.insert(5, 500);
    map.insert(2, 200);
    map.insert(4, 400);

    MyString str{"hello natvis", 12};
    MyU16String wstr{u"wide text", 9};

    ColorHolder colors{Color::Green, (FlagBits)(F_A | F_C)};

    Temperature temp{20.0f};

    Circle circle{};
    circle.id = 7;
    circle.radius = 2.5f;
    Square square{};
    square.id = 8;
    square.side = 4;

    SelfExpand self{11, 22};

    OptHolder optZero{0, 42};
    OptHolder optOther{1, 43};

    Vec3 v3{3.0f, 4.0f, 0.0f};

    Deque dq{};
    dq.start = 6;
    dq.len = 4;
    dq.ring[6] = 100;
    dq.ring[7] = 200;
    dq.ring[0] = 300;
    dq.ring[1] = 400;

    MyHashTable table{};
    table.put(1, 111);
    table.put(9, 999);   // collides with 1 (9 & 7 == 1)
    table.put(4, 444);

    Both both{77};
    AliasVector alias{55};

    ViewHolder views{{5, 6}};

    int backing[4] = {1, 2, 3, 4};
    BadSize badsize{-3, backing};
    RecIntr recintr{9};

    volatile int stop = 0;  // BREAK HERE
    (void)stop;
    printf("%d %d %d\n", vec.mSize + aliasRef.mSize, map.size, table.count);
    return 0;
}
